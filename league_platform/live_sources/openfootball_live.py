"""Allowlisted OpenFootball current schedule/result adapter.

OpenFootball is the fact source.  HTTP (or a separately configured crawling
runtime) is only the transport layer and never replaces the repository URL,
observation time, payload hash, licence, or row pointer kept on every fixture.

The fixed 2026/27 sources use two OpenFootball serialisations: ``football.json``
JSON and Football.TXT.  Football.TXT omits repeated dates and repeated kickoff
times inside a time group.  Those values are inherited according to the source
grammar; a match for which no time group exists remains date-only and is never
assigned a synthetic midnight/noon kickoff.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from league_platform.live_sources.http_utils import read_response_bounded
from league_platform.openfootball_raw_archive import (
    OpenFootballRawObservation,
    validate_openfootball_raw_receipt,
)
from league_platform.source_rights import SourceId, UseCase, require_source_rights


MAX_CONTENT_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 4
MAX_TRANSPORT_RETRIES_PER_SOURCE = 1
DEFAULT_TIMEOUT_SECONDS = 20.0
RESPONSE_READ_TIMEOUT_SECONDS = 20.0
SOURCE_LICENSE = "CC0-1.0"


# The URLs are deliberately exact rather than host/path patterns.  Adding a
# new season or competition is an explicit source-contract change.
OPENFOOTBALL_SOURCES: dict[str, dict[str, str]] = {
    "openfootball:football.json:2026-27:en.1": {
        "source_id": "openfootball:football.json:2026-27:en.1",
        "competition_id": "premier-league",
        "season": "2026-27",
        "format": "json",
        "timezone": "Europe/London",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
    },
    "openfootball:football.json:2026-27:en.2": {
        "source_id": "openfootball:football.json:2026-27:en.2",
        "competition_id": "championship",
        "season": "2026-27",
        "format": "json",
        "timezone": "Europe/London",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.2.json",
    },
    "openfootball:deutschland:2026-27:1-bundesliga": {
        "source_id": "openfootball:deutschland:2026-27:1-bundesliga",
        "competition_id": "bundesliga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Berlin",
        "url": "https://raw.githubusercontent.com/openfootball/deutschland/master/2026-27/1-bundesliga.txt",
    },
    "openfootball:espana:2026-27:1-liga": {
        "source_id": "openfootball:espana:2026-27:1-liga",
        "competition_id": "la-liga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Madrid",
        "url": "https://raw.githubusercontent.com/openfootball/espana/master/2026-27/1-liga.txt",
    },
    "openfootball:italy:2026-27:1-seriea": {
        "source_id": "openfootball:italy:2026-27:1-seriea",
        "competition_id": "serie-a",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Rome",
        "url": "https://raw.githubusercontent.com/openfootball/italy/master/2026-27/1-seriea.txt",
    },
    "openfootball:europe:france:2026-27-fr1": {
        "source_id": "openfootball:europe:france:2026-27-fr1",
        "competition_id": "ligue-1",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Paris",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/france/2026-27_fr1.txt",
    },
    "openfootball:europe:netherlands:2026-27-nl1": {
        "source_id": "openfootball:europe:netherlands:2026-27-nl1",
        "competition_id": "eredivisie",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Amsterdam",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/netherlands/2026-27_nl1.txt",
    },
    "openfootball:europe:portugal:2026-27-pt1": {
        "source_id": "openfootball:europe:portugal:2026-27-pt1",
        "competition_id": "primeira-liga",
        "season": "2026-27",
        "format": "txt",
        "timezone": "Europe/Lisbon",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/portugal/2026-27_pt1.txt",
    },
}
OPENFOOTBALL_CURRENT_SOURCE_IDS = tuple(OPENFOOTBALL_SOURCES)
OPENFOOTBALL_HISTORY_SEASONS = (
    "2015-16",
    "2016-17",
    "2017-18",
    "2018-19",
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
)
_OPENFOOTBALL_HISTORY_SERIES = (
    (
        "premier-league",
        "england",
        "1-premierleague",
        "{season}/1-premierleague.txt",
        "Europe/London",
    ),
    (
        "championship",
        "england",
        "2-championship",
        "{season}/2-championship.txt",
        "Europe/London",
    ),
    (
        "bundesliga",
        "deutschland",
        "1-bundesliga",
        "{season}/1-bundesliga.txt",
        "Europe/Berlin",
    ),
    (
        "la-liga",
        "espana",
        "1-liga",
        "{season}/1-liga.txt",
        "Europe/Madrid",
    ),
    (
        "serie-a",
        "italy",
        "1-seriea",
        "{season}/1-seriea.txt",
        "Europe/Rome",
    ),
    (
        "ligue-1",
        "europe",
        "france-fr1",
        "france/{season}_fr1.txt",
        "Europe/Paris",
    ),
)


def _build_history_sources() -> dict[str, dict[str, str]]:
    sources: dict[str, dict[str, str]] = {}
    for season in OPENFOOTBALL_HISTORY_SEASONS:
        for (
            competition_id,
            repository,
            token,
            path_template,
            timezone_name,
        ) in _OPENFOOTBALL_HISTORY_SERIES:
            source_id = f"openfootball:{repository}:{season}:{token}"
            path = path_template.format(season=season)
            sources[source_id] = {
                "source_id": source_id,
                "competition_id": competition_id,
                "season": season,
                "format": "txt",
                "timezone": timezone_name,
                "url": (
                    f"https://raw.githubusercontent.com/openfootball/{repository}/master/{path}"
                ),
            }
    return sources


OPENFOOTBALL_HISTORY_SOURCES = _build_history_sources()
OPENFOOTBALL_HISTORY_SOURCE_IDS = tuple(OPENFOOTBALL_HISTORY_SOURCES)
OPENFOOTBALL_SOURCES = {
    **OPENFOOTBALL_SOURCES,
    **OPENFOOTBALL_HISTORY_SOURCES,
}
OPENFOOTBALL_ALLOWED_URLS = frozenset(config["url"] for config in OPENFOOTBALL_SOURCES.values())


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_TXT_DATE_RE = re.compile(
    r"^\s{0,2}(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})(?:\s+(?P<year>\d{4}))?\s*$"
)
_TXT_ROUND_RE = re.compile(r"^\s*[▪»]\s*(?P<round>.+?)\s*$")
_TXT_MATCH_RE = re.compile(
    r"^\s{4}(?:(?P<time>\d{1,2}:\d{2})\s{2})?"
    r"(?P<home>.+?)\s+v\s+(?P<away_and_score>.+?)\s*$"
)
_TXT_HISTORICAL_RESULT_RE = re.compile(
    r"^\s{2,}(?:(?P<time>\d{1,2}:\d{2})\s{2,})?"
    r"(?P<home>.+?)\s{2,}(?P<home_goals>\d+)-(?P<away_goals>\d+)"
    r"(?:\s+\((?P<ht_home>\d+)-(?P<ht_away>\d+)\))?"
    r"\s{2,}(?P<away>.+?)\s*$"
)
_TXT_SCORE_RE = re.compile(
    r"^(?P<away>.+?)\s+(?P<home_goals>\d+)-(?P<away_goals>\d+)"
    r"(?:\s+\((?P<ht_home>\d+)-(?P<ht_away>\d+)\))?\s*$"
)
_MONTHS = {
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


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _source_config(*, source_id: str, url: str, expected_format: str) -> dict[str, str]:
    config = OPENFOOTBALL_SOURCES.get(source_id)
    if config is None:
        raise ValueError("OpenFootball source_id is not allowlisted")
    if url != config["url"] or url not in OPENFOOTBALL_ALLOWED_URLS:
        raise ValueError("OpenFootball URL is not allowlisted for source_id")
    if config["format"] != expected_format:
        raise ValueError(f"OpenFootball source {source_id} is not {expected_format}")
    return config


def _validate_payload(payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("OpenFootball response must be bytes")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("OpenFootball response exceeded 4 MiB")


def _source_metadata(
    *, payload: bytes, config: dict[str, str], retrieved_at: datetime
) -> dict[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "name": "OpenFootball",
        "source_id": config["source_id"],
        "url": config["url"],
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": digest,
        "hash": f"sha256:{digest}",
        "license": SOURCE_LICENSE,
    }


def _lineage(
    source: dict[str, str],
    *,
    parser: str,
    record_index: int,
    line_number: int,
    record_path: str,
    date_inherited: bool = False,
    time_inherited: bool = False,
) -> dict:
    return {
        "source_id": source["source_id"],
        "url": source["url"],
        "retrieved_at": source["retrieved_at"],
        "raw_sha256": source["raw_sha256"],
        "hash": source["hash"],
        "license": source["license"],
        "parser": parser,
        "record_index": record_index,
        "line_number": line_number,
        "record_path": record_path,
        "source_row_id": f"{source['source_id']}#{record_path}",
        "date_inherited": date_inherited,
        "time_inherited": time_inherited,
    }


def _normalise_identity(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _identity_phase(round_name: str | None) -> str | None:
    normalised = _normalise_identity(round_name or "")
    if any(marker in normalised for marker in ("playoff", "semi-final", "semifinal", "final")):
        return f"postseason:{normalised}"
    return None


def _fixture_id(
    *,
    config: dict[str, str],
    home: str,
    away: str,
    round_name: str | None = None,
) -> str:
    # Date, kickoff time, score and ordinary matchday labels are deliberately
    # excluded so routine corrections or rescheduling do not mint a second
    # identity.  Postseason rounds are included because Championship playoff
    # rematches can repeat an already played regular-season home/away pairing.
    identity_parts = [
        config["source_id"],
        config["season"],
        _normalise_identity(home),
        _normalise_identity(away),
    ]
    phase = _identity_phase(round_name)
    if phase is not None:
        identity_parts.append(phase)
    identity = "|".join(identity_parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"openfootball:{config['competition_id']}:{digest}"


def _parse_iso_date(value: object, *, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date") from exc


def _parse_time(value: object, *, field: str) -> time:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} is not a valid local time")
    hour, minute = (int(part) for part in value.strip().split(":"))
    return time(hour, minute)


def _local_kickoff_utc(kickoff_date: date, time_value: str, *, timezone_name: str) -> str:
    local_time = _parse_time(time_value, field="kickoff time")
    zone = ZoneInfo(timezone_name)
    naive = datetime.combine(kickoff_date, local_time)

    # ZoneInfo accepts nonexistent wall-clock times by attaching the previous
    # offset.  Round-trip validation prevents that silent fabrication.  A
    # fall-back overlap is also ambiguous without a source offset, so fail
    # closed instead of selecting a fold arbitrarily.
    candidates: list[datetime] = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        utc_value = local.astimezone(timezone.utc)
        round_trip = utc_value.astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and all(utc_value != item for item in candidates):
            candidates.append(utc_value)
    if not candidates:
        raise ValueError(
            f"OpenFootball kickoff {kickoff_date} {time_value} is nonexistent in {timezone_name}"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"OpenFootball kickoff {kickoff_date} {time_value} is ambiguous in {timezone_name}"
        )
    return candidates[0].isoformat()


def _score_pair(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    home, away = value
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


def _json_scores(value: object, *, record_index: int) -> tuple[dict | None, dict | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        final_value = value.get("ft")
        if final_value is None:
            return None, None
        final = _score_pair(final_value)
        halftime = _score_pair(value.get("ht"))
    else:
        final = _score_pair(value)
        halftime = None
    if final is None:
        raise ValueError(f"OpenFootball JSON match {record_index} has an invalid score")
    if halftime is not None and (
        halftime["home"] > final["home"] or halftime["away"] > final["away"]
    ):
        halftime = None
    return final, halftime


def _json_team_line_numbers(text: str) -> list[int]:
    return [text.count("\n", 0, match.start()) + 1 for match in re.finditer(r'"team1"\s*:', text)]


def _fixture_record(
    *,
    config: dict[str, str],
    source: dict[str, str],
    round_name: str | None,
    kickoff_date: date,
    time_value: str | None,
    time_inherited: bool,
    home: str,
    away: str,
    score: dict | None,
    halftime_score: dict | None,
    lineage: dict,
) -> dict:
    fixture_id = _fixture_id(
        config=config,
        home=home,
        away=away,
        round_name=round_name,
    )
    record = {
        "id": fixture_id,
        "provider_fixture_ids": {"OpenFootball": fixture_id},
        "competition_id": config["competition_id"],
        "season": config["season"],
        "round": round_name,
        "kickoff_date": kickoff_date.isoformat(),
        "kickoff_time_quality": "exact" if time_value else "date_only",
        "kickoff_time_source": (
            "group_inherited"
            if time_value and time_inherited
            else "explicit"
            if time_value
            else "missing"
        ),
        "kickoff_timezone": config["timezone"],
        "home_team": home,
        "away_team": away,
        "status": "finished" if score is not None else "upcoming",
        "score": score,
        "halftime_score": halftime_score,
        "result_scope": "regulation_90" if score is not None else None,
        "source": source,
        "lineage": lineage,
    }
    if time_value:
        record["kickoff_at"] = _local_kickoff_utc(
            kickoff_date,
            time_value,
            timezone_name=config["timezone"],
        )
    return record


def parse_openfootball_json(
    payload: bytes,
    *,
    source_id: str,
    retrieved_at: datetime,
    url: str,
    competition_id: str | None = None,
    season: str | None = None,
    timezone_name: str | None = None,
) -> list[dict]:
    """Parse one allowlisted ``football.json`` season response."""

    config = _source_config(source_id=source_id, url=url, expected_format="json")
    _validate_optional_config(
        config,
        competition_id=competition_id,
        season=season,
        timezone_name=timezone_name,
    )
    _validate_payload(payload)
    observed = _utc(retrieved_at, field="retrieved_at")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("OpenFootball JSON response is not UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenFootball response is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise ValueError("OpenFootball JSON response must contain a matches list")

    source = _source_metadata(payload=payload, config=config, retrieved_at=observed)
    team_lines = _json_team_line_numbers(text)
    rows: list[dict] = []
    current_date: date | None = None
    current_round: str | None = None
    for index, item in enumerate(data["matches"]):
        if not isinstance(item, dict):
            continue
        raw_date = item.get("date")
        date_inherited = raw_date in (None, "")
        if date_inherited:
            if current_date is None:
                continue
            match_date = current_date
        else:
            match_date = _parse_iso_date(raw_date, field=f"matches[{index}].date")
            current_date = match_date

        raw_round = item.get("round")
        if isinstance(raw_round, str) and raw_round.strip():
            current_round = raw_round.strip()
        home = str(item.get("team1") or "").strip()
        away = str(item.get("team2") or "").strip()
        if not home or not away:
            continue
        raw_time = item.get("time")
        time_value = None
        if raw_time not in (None, ""):
            _parse_time(raw_time, field=f"matches[{index}].time")
            time_value = str(raw_time).strip()
        score, halftime = _json_scores(item.get("score"), record_index=index)
        line_number = team_lines[index] if index < len(team_lines) else 1
        lineage = _lineage(
            source,
            parser="openfootball_json_v1",
            record_index=index,
            line_number=line_number,
            record_path=f"$.matches[{index}]",
            date_inherited=date_inherited,
        )
        rows.append(
            _fixture_record(
                config=config,
                source=source,
                round_name=current_round,
                kickoff_date=match_date,
                time_value=time_value,
                time_inherited=False,
                home=home,
                away=away,
                score=score,
                halftime_score=halftime,
                lineage=lineage,
            )
        )
    return rows


def _validate_optional_config(
    config: dict[str, str],
    *,
    competition_id: str | None,
    season: str | None,
    timezone_name: str | None,
) -> None:
    expected = {
        "competition_id": competition_id,
        "season": season,
        "timezone": timezone_name,
    }
    for key, supplied in expected.items():
        if supplied is not None and supplied != config[key]:
            raise ValueError(f"OpenFootball {key} does not match allowlisted source")


def _txt_date(match: re.Match[str], *, current_date: date | None, season: str) -> date:
    month = _MONTHS[match.group("month")]
    day = int(match.group("day"))
    explicit_year = match.group("year")
    if explicit_year:
        year = int(explicit_year)
    elif current_date is None:
        year = int(season.split("-", 1)[0])
    else:
        year = current_date.year
        if current_date.month >= 10 and month <= 3:
            year += 1
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("OpenFootball Football.TXT contains an invalid date") from exc


def _txt_score(away_and_score: str) -> tuple[str, dict | None, dict | None]:
    score_match = _TXT_SCORE_RE.fullmatch(away_and_score.strip())
    if score_match is None:
        return away_and_score.strip(), None, None
    away = score_match.group("away").strip()
    final = {
        "home": int(score_match.group("home_goals")),
        "away": int(score_match.group("away_goals")),
    }
    halftime = None
    if score_match.group("ht_home") is not None:
        candidate = {
            "home": int(score_match.group("ht_home")),
            "away": int(score_match.group("ht_away")),
        }
        if candidate["home"] <= final["home"] and candidate["away"] <= final["away"]:
            halftime = candidate
    return away, final, halftime


def parse_openfootball_txt(
    payload: bytes,
    *,
    source_id: str,
    retrieved_at: datetime,
    url: str,
    competition_id: str | None = None,
    season: str | None = None,
    timezone_name: str | None = None,
) -> list[dict]:
    """Parse one allowlisted Football.TXT season response."""

    config = _source_config(source_id=source_id, url=url, expected_format="txt")
    _validate_optional_config(
        config,
        competition_id=competition_id,
        season=season,
        timezone_name=timezone_name,
    )
    _validate_payload(payload)
    observed = _utc(retrieved_at, field="retrieved_at")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("OpenFootball Football.TXT response is not UTF-8") from exc
    source = _source_metadata(payload=payload, config=config, retrieved_at=observed)

    rows: list[dict] = []
    current_date: date | None = None
    current_group_time: str | None = None
    current_round: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        round_match = _TXT_ROUND_RE.fullmatch(line)
        if round_match:
            current_round = round_match.group("round").strip()
            current_group_time = None
            continue
        date_match = _TXT_DATE_RE.fullmatch(line)
        if date_match:
            current_date = _txt_date(
                date_match,
                current_date=current_date,
                season=config["season"],
            )
            current_group_time = None
            continue
        match = _TXT_MATCH_RE.fullmatch(line)
        historical_result = _TXT_HISTORICAL_RESULT_RE.fullmatch(line) if match is None else None
        if match is None and historical_result is None:
            continue
        if current_date is None:
            raise ValueError(f"OpenFootball Football.TXT match at line {line_number} has no date")
        matched_row = match if match is not None else historical_result
        assert matched_row is not None
        explicit_time = matched_row.group("time")
        if explicit_time is not None:
            _parse_time(explicit_time, field=f"line {line_number} kickoff time")
            current_group_time = explicit_time
        time_value = explicit_time or current_group_time
        time_inherited = explicit_time is None and current_group_time is not None
        home = matched_row.group("home").strip()
        if historical_result is None:
            assert match is not None
            away, score, halftime = _txt_score(match.group("away_and_score"))
        else:
            away = historical_result.group("away").strip()
            score = {
                "home": int(historical_result.group("home_goals")),
                "away": int(historical_result.group("away_goals")),
            }
            halftime = None
            if historical_result.group("ht_home") is not None:
                candidate = {
                    "home": int(historical_result.group("ht_home")),
                    "away": int(historical_result.group("ht_away")),
                }
                if candidate["home"] <= score["home"] and candidate["away"] <= score["away"]:
                    halftime = candidate
        if not home or not away:
            continue
        record_index = len(rows)
        lineage = _lineage(
            source,
            parser="openfootball_football_txt_v1",
            record_index=record_index,
            line_number=line_number,
            record_path=f"L{line_number}",
            date_inherited=True,
            time_inherited=time_inherited,
        )
        rows.append(
            _fixture_record(
                config=config,
                source=source,
                round_name=current_round,
                kickoff_date=current_date,
                time_value=time_value,
                time_inherited=time_inherited,
                home=home,
                away=away,
                score=score,
                halftime_score=halftime,
                lineage=lineage,
            )
        )
    return rows


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("OpenFootball redirected; redirects are disabled")


def _safe_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _read_source(
    config: dict[str, str],
    *,
    opener: Callable[..., object] | object | None,
) -> bytes:
    request = urllib.request.Request(
        config["url"],
        headers={
            "Accept": "application/json, text/plain;q=0.9",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    client = opener if opener is not None else _safe_opener()
    open_method = getattr(client, "open", None) or client
    response = open_method(request, timeout=DEFAULT_TIMEOUT_SECONDS)  # type: ignore[operator]
    with response:
        final_url = str(getattr(response, "geturl", lambda: request.full_url)())
        if final_url != request.full_url:
            raise ValueError("OpenFootball response redirected; redirects are disabled")
        if final_url not in OPENFOOTBALL_ALLOWED_URLS:
            raise ValueError("OpenFootball response URL is not allowlisted")
        payload = read_response_bounded(
            response,
            MAX_CONTENT_BYTES,
            timeout_seconds=RESPONSE_READ_TIMEOUT_SECONDS,
        )
    _validate_payload(payload)
    return payload


def _selected_sources(source_ids: Iterable[str] | None) -> list[dict[str, str]]:
    if source_ids is None:
        selected_ids = list(OPENFOOTBALL_CURRENT_SOURCE_IDS)
    elif isinstance(source_ids, str):
        selected_ids = [source_ids]
    else:
        selected_ids = list(source_ids)
    if not selected_ids:
        raise ValueError("OpenFootball source_ids must not be empty")
    unknown = [value for value in selected_ids if value not in OPENFOOTBALL_SOURCES]
    if unknown:
        raise ValueError(f"OpenFootball source_id is not allowlisted: {unknown[0]}")
    return [OPENFOOTBALL_SOURCES[value] for value in dict.fromkeys(selected_ids)]


class _OpenFootballSourceFailure(Exception):
    def __init__(
        self,
        cause: Exception,
        *,
        transport_retry_count: int,
        stage: str = "fetch_or_parse",
    ) -> None:
        super().__init__(str(cause))
        self.transport_retry_count = transport_retry_count
        self.stage = stage


def _is_transient_transport_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(
        exc,
        (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ssl.SSLError,
        ),
    )


def _fetch_one(
    config: dict[str, str],
    *,
    retrieved_at: datetime,
    opener: Callable[..., object] | object | None,
    raw_sink: Callable[[OpenFootballRawObservation], Mapping[str, Any]] | None = None,
) -> tuple[list[dict], int] | tuple[list[dict], int, dict[str, Any]]:
    transport_retry_count = 0
    while True:
        try:
            payload = _read_source(config, opener=opener)
            break
        except Exception as exc:
            if (
                transport_retry_count < MAX_TRANSPORT_RETRIES_PER_SOURCE
                and _is_transient_transport_error(exc)
            ):
                transport_retry_count += 1
                continue
            raise _OpenFootballSourceFailure(
                exc,
                transport_retry_count=transport_retry_count,
            ) from exc
    archive_receipt: dict[str, Any] | None = None
    if raw_sink is not None:
        observation = OpenFootballRawObservation(
            source_id=config["source_id"],
            url=config["url"],
            retrieved_at=retrieved_at,
            payload=payload,
            source_format=config["format"],
            competition_id=config["competition_id"],
            season=config["season"],
            timezone_name=config["timezone"],
            license=SOURCE_LICENSE,
        )
        try:
            archive_receipt = validate_openfootball_raw_receipt(
                observation,
                raw_sink(observation),
            )
        except Exception as exc:
            raise _OpenFootballSourceFailure(
                exc,
                transport_retry_count=transport_retry_count,
                stage="raw_archive",
            ) from exc
    parser = parse_openfootball_json if config["format"] == "json" else parse_openfootball_txt
    try:
        rows = parser(
            payload,
            source_id=config["source_id"],
            retrieved_at=retrieved_at,
            url=config["url"],
        )
    except Exception as exc:
        raise _OpenFootballSourceFailure(
            exc,
            transport_retry_count=transport_retry_count,
        ) from exc
    if archive_receipt is not None:
        return rows, transport_retry_count, archive_receipt
    return rows, transport_retry_count


def _kickoff_value(fixture: dict) -> datetime:
    value = fixture.get("kickoff_at")
    if not isinstance(value, str):
        raise ValueError("exact OpenFootball fixture is missing kickoff_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("OpenFootball fixture kickoff_at is invalid") from exc
    return _utc(parsed, field="kickoff_at")


def fetch_openfootball_current(
    *,
    now: datetime | None = None,
    source_ids: Iterable[str] | None = None,
    opener: Callable[..., object] | object | None = None,
    raw_sink: Callable[[OpenFootballRawObservation], Mapping[str, Any]] | None = None,
) -> dict:
    """Fetch the fixed current-season sources with at most four workers.

    ``fixtures`` contains only schedulable, timezone-aware rows.  Parsed rows
    without a source time are retained separately in ``date_only_fixtures``.
    The three convenience windows are derived only from the exact collection.
    """

    rights = require_source_rights(
        SourceId.OPENFOOTBALL_CURRENT,
        UseCase.NETWORK_FETCH,
    )
    reference = _utc(now or datetime.now(timezone.utc), field="now")
    selected = _selected_sources(source_ids)
    rows: list[dict] = []
    errors: list[dict] = []
    successful_source_count = 0
    transport_retry_count = 0
    retried_source_count = 0
    raw_archive_receipts: list[dict[str, Any]] = []
    worker_count = min(MAX_CONCURRENT_REQUESTS, len(selected))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for config in selected:
            fetch_kwargs: dict[str, Any] = {
                "retrieved_at": reference,
                "opener": opener,
            }
            if raw_sink is not None:
                fetch_kwargs["raw_sink"] = raw_sink
            futures[executor.submit(_fetch_one, config, **fetch_kwargs)] = config
        for future in as_completed(futures):
            config = futures[future]
            try:
                source_result = future.result()
                source_rows, source_retry_count = source_result[:2]
                if len(source_result) == 3:
                    raw_archive_receipts.append(source_result[2])
                rows.extend(source_rows)
                successful_source_count += 1
                transport_retry_count += source_retry_count
                retried_source_count += int(source_retry_count > 0)
            except Exception as exc:  # each source is isolated from the others
                source_retry_count = (
                    exc.transport_retry_count if isinstance(exc, _OpenFootballSourceFailure) else 0
                )
                transport_retry_count += source_retry_count
                retried_source_count += int(source_retry_count > 0)
                errors.append(
                    {
                        "source_id": config["source_id"],
                        "competition_id": config["competition_id"],
                        "url": config["url"],
                        "stage": (
                            exc.stage
                            if isinstance(exc, _OpenFootballSourceFailure)
                            else "fetch_or_parse"
                        ),
                        "error": str(exc),
                        "transport_retry_count": source_retry_count,
                    }
                )

    exact = [
        row
        for row in rows
        if row.get("kickoff_time_quality") == "exact" and isinstance(row.get("kickoff_at"), str)
    ]
    date_only = [row for row in rows if row.get("kickoff_time_quality") == "date_only"]
    exact.sort(key=lambda row: (_kickoff_value(row), row["id"]))
    date_only.sort(key=lambda row: (row["kickoff_date"], row["id"]))
    errors.sort(key=lambda row: row["source_id"])
    raw_archive_receipts.sort(key=lambda row: (row["source_id"], row["retrieved_at"]))

    recent_start = reference - timedelta(days=7)
    upcoming_3_end = reference + timedelta(days=3)
    upcoming_7_end = reference + timedelta(days=7)
    recent_results = [
        row
        for row in exact
        if row.get("status") == "finished" and recent_start <= _kickoff_value(row) <= reference
    ]
    upcoming_3_days = [
        row
        for row in exact
        if row.get("status") == "upcoming" and reference <= _kickoff_value(row) <= upcoming_3_end
    ]
    upcoming_7_days = [
        row
        for row in exact
        if row.get("status") == "upcoming" and reference <= _kickoff_value(row) <= upcoming_7_end
    ]
    any_rows = bool(exact or date_only)
    status = "ok" if any_rows and not errors else "degraded" if any_rows else "unavailable"
    diagnostics = {
        "selected_source_count": len(selected),
        "successful_source_count": successful_source_count,
        "failed_source_count": len(errors),
        "parsed_fixture_count": len(rows),
        "exact_fixture_count": len(exact),
        "date_only_fixture_count": len(date_only),
        "finished_fixture_count": sum(row.get("status") == "finished" for row in rows),
        "upcoming_fixture_count": sum(row.get("status") == "upcoming" for row in rows),
        "isolated_error_count": len(errors),
        "transport_retry_count": transport_retry_count,
        "retried_source_count": retried_source_count,
        "archived_source_count": len(raw_archive_receipts),
    }
    admission_status = (
        "candidate_only_unarchived"
        if raw_sink is None
        else "raw_observation_archived"
        if len(raw_archive_receipts) == len(selected)
        else "raw_observation_partially_archived"
    )
    return {
        "provider": "OpenFootball",
        "retrieved_at": reference.isoformat(),
        "fixtures": exact,
        "date_only_fixtures": date_only,
        "recent_results": recent_results,
        "upcoming_3_days": upcoming_3_days,
        "upcoming_7_days": upcoming_7_days,
        "errors": errors,
        "status": status,
        "training_admitted": False,
        "raw_archive_receipts": raw_archive_receipts,
        "source_contract": {
            "fact_source": "OpenFootball",
            "rights": rights.as_dict(),
            "transport_role": "fetch execution only; never the fact source",
            "license": SOURCE_LICENSE,
            "source_ids": [config["source_id"] for config in selected],
            "allowlisted_urls": [config["url"] for config in selected],
            "redirects": "disabled",
            "max_response_bytes": MAX_CONTENT_BYTES,
            "max_concurrency": MAX_CONCURRENT_REQUESTS,
            "max_transport_retries_per_source": MAX_TRANSPORT_RETRIES_PER_SOURCE,
            "retry_policy": "one_retry_for_transient_transport_errors_only",
            "date_only_policy": "quarantined_from_schedulable_fixtures_and_time_windows",
            "row_lineage": "source_id+url+retrieved_at+sha256+license+record_pointer",
            "admission_status": admission_status,
        },
        "diagnostics": diagnostics,
    }


def fetch_openfootball_history(
    *,
    now: datetime | None = None,
    source_ids: Iterable[str] | None = None,
    opener: Callable[..., object] | object | None = None,
    raw_sink: Callable[[OpenFootballRawObservation], Mapping[str, Any]] | None = None,
) -> dict:
    """Fetch the exact 2015/16--2025/26 CC0 training-source allowlist.

    The current adapter remains a six-source operation.  Historical fan-out is
    opt-in and keeps any unfinished row quarantined from the training corpus.
    """

    rights = require_source_rights(
        SourceId.OPENFOOTBALL_HISTORICAL,
        UseCase.NETWORK_FETCH,
    )
    selected_ids = OPENFOOTBALL_HISTORY_SOURCE_IDS if source_ids is None else source_ids
    result = fetch_openfootball_current(
        now=now,
        source_ids=selected_ids,
        opener=opener,
        raw_sink=raw_sink,
    )
    all_rows = [*result["fixtures"], *result["date_only_fixtures"]]
    history = sorted(
        (row for row in all_rows if row.get("status") == "finished"),
        key=lambda row: (row["kickoff_date"], row["id"]),
    )
    unfinished = sorted(
        (row for row in all_rows if row.get("status") != "finished"),
        key=lambda row: (row["kickoff_date"], row["id"]),
    )
    output = dict(result)
    output["history"] = history
    output["unfinished_fixtures"] = unfinished
    output["history_fixture_count"] = len(history)
    output["unfinished_fixture_count"] = len(unfinished)
    output["status"] = "degraded" if unfinished and result["status"] == "ok" else result["status"]
    output["source_contract"] = {
        **result["source_contract"],
        "rights": rights.as_dict(),
        "scope": "historical_training_corpus",
        "season_start": OPENFOOTBALL_HISTORY_SEASONS[0],
        "season_end": OPENFOOTBALL_HISTORY_SEASONS[-1],
        "unfinished_policy": "quarantined_from_training",
    }
    return output


def fetch_openfootball_fixtures(**kwargs: Any) -> dict:
    """Compatibility name for callers that use the fixture-adapter pattern."""

    return fetch_openfootball_current(**kwargs)


def parse_openfootball_json_payload(payload: bytes, **kwargs: Any) -> list[dict]:
    """Compatibility name matching other live-source parser modules."""

    return parse_openfootball_json(payload, **kwargs)


def parse_openfootball_txt_payload(payload: bytes, **kwargs: Any) -> list[dict]:
    """Compatibility name matching other live-source parser modules."""

    return parse_openfootball_txt(payload, **kwargs)


__all__ = [
    "MAX_CONCURRENT_REQUESTS",
    "MAX_CONTENT_BYTES",
    "OPENFOOTBALL_ALLOWED_URLS",
    "OPENFOOTBALL_CURRENT_SOURCE_IDS",
    "OPENFOOTBALL_HISTORY_SEASONS",
    "OPENFOOTBALL_HISTORY_SOURCE_IDS",
    "OPENFOOTBALL_HISTORY_SOURCES",
    "OPENFOOTBALL_SOURCES",
    "SOURCE_LICENSE",
    "fetch_openfootball_current",
    "fetch_openfootball_fixtures",
    "fetch_openfootball_history",
    "parse_openfootball_json",
    "parse_openfootball_json_payload",
    "parse_openfootball_txt",
    "parse_openfootball_txt_payload",
]
