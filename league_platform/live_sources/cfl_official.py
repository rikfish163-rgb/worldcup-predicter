"""First-party Chinese Super League schedule and result adapter.

The Chinese Professional Football League (CFL) website is the fact source.
This adapter calls only the public JSON interface used by that website, over
strict HTTPS, with redirects disabled and bounded response reads.  Public
access is not represented as a commercial reuse licence: every row keeps the
unverified reuse-rights state alongside its observation time and raw hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from league_platform.live_sources.http_utils import read_response_bounded
from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


CFL_API_HOST = "api.cfl-china.cn"
CFL_API_BASE_URL = f"https://{CFL_API_HOST}"
CFL_OFFICIAL_PAGE_URL = "https://www.cfl-china.cn/zh/fixtures/list.html?competition_code=CSL"
TOURNAMENTS_PATH = "/frontweb/api/tournaments"
STAGE_PATH = "/frontweb/api/matches/select/stage"
MATCHES_PATH = "/frontweb/api/matches/page"
ALLOWED_PATHS = frozenset({TOURNAMENTS_PATH, STAGE_PATH, MATCHES_PATH})

SOURCE_NAME = "Chinese Professional Football League official"
SOURCE_LICENSE = "not_published"
SOURCE_RIGHTS_STATUS = "public_first_party_access_reuse_terms_unverified"
SOURCE_ACCESS_BASIS = "official_public_frontend_api"
MAX_CONTENT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
RESPONSE_READ_TIMEOUT_SECONDS = 20.0
MAX_REQUEST_ATTEMPTS = 2
MAX_SEASON_MATCHES = 400
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_NATIVE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# These mappings are deliberately exact pairs, not substring heuristics and
# never a home-team inference.  The English label itself names the city, so
# the dynamic CFL row remains the field-level source.
_EXPLICIT_VENUE_CITY_BY_OFFICIAL_LABEL = {
    ("重庆龙兴足球场", "Chongqing Longxing Football Stadium"): "Chongqing",
    ("大连梭鱼湾足球场", "Dalian Suoyuwan Football Stadium"): "Dalian",
    ("济南奥体中心体育场", "Jinan Olympic Sports Center"): "Jinan",
    (
        "青岛西海岸大学城体育场",
        "Qingdao West Coast University City Stadium",
    ): "Qingdao",
    ("青岛青春足球场", "Qingdao Youth Football Stadium"): "Qingdao",
    ("上海体育场", "Shanghai Stadium"): "Shanghai",
    ("深圳市体育场", "Shenzhen Stadium"): "Shenzhen",
    ("天津奥体中心体育场", "Tianjin Olympic Center Stadium"): "Tianjin",
    ("武汉体育中心体育场", "Wuhan Sports Center Stadium"): "Wuhan",
    ("玉溪高原体育运动中心体育场", "Yuxi Plateau Sports Center Stadium"): "Yuxi",
    ("郑州航海体育场", "Zhengzhou Hanghai Stadium"): "Zhengzhou",
}

# CFL does not publish a separate city field.  These three non-explicit labels
# are therefore allowed only through a manually reviewed first-party registry.
# Hashes are for the exact public HTML fetched on 2026-08-24 with normal HTTPS;
# they are evidence identifiers, not a claim of commercial reuse permission.
_CURATED_VENUE_CITY_BY_OFFICIAL_LABEL: dict[tuple[str, str], dict[str, str | bool]] = {
    ("黄龙体育中心体育场", "Huanglong Sports Centre"): {
        "city": "Hangzhou",
        "name": "General Administration of Sport of China",
        "url": "https://www.sport.gov.cn/n14471/n14482/n14519/c930951/content.html",
        "raw_sha256": "5ee5616474038ff61db3bdd13605f21a4b59cb11a15166a70914da2b312f7971",
        "reviewed_on": "2026-08-24",
        "rights_status": "public_first_party_access_reuse_terms_unverified",
        "commercial_reuse_verified": False,
    },
    ("泰达足球场", "TEDA Football Stadium"): {
        "city": "Tianjin",
        "name": "Tianjin Economic-Technological Development Area official",
        "url": "https://www.teda.gov.cn/contents/21/68913.html",
        "raw_sha256": "36a6fd2f83fb77f19328248cc560a4537c0f8024c99a75491dff04dcb5d114f2",
        "reviewed_on": "2026-08-24",
        "rights_status": "public_first_party_access_reuse_terms_unverified",
        "commercial_reuse_verified": False,
    },
    ("五粮液文化体育中心专业足球场", "Wuliangye Sports Center Stadium"): {
        "city": "Chengdu",
        "name": "General Administration of Sport of China",
        "url": "https://www.sport.gov.cn/n14471/n14494/n14544/c29628458/content.html",
        "raw_sha256": "f07afd381ed8468c7e3a9e01792b9b68fef9ef01afdfdc27208e8bf75ae69f77",
        "reviewed_on": "2026-08-24",
        "rights_status": "public_first_party_access_reuse_terms_unverified",
        "commercial_reuse_verified": False,
    },
}


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_payload(payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("CFL official response must be bytes")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("CFL official response exceeded 4 MiB")


def _validate_api_url(url: str, *, expected_path: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CFL_API_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != expected_path
        or parsed.path not in ALLOWED_PATHS
    ):
        raise ValueError("CFL official API URL is not allowlisted")


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    _validate_payload(payload)
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"CFL official {label} response is not UTF-8") from exc
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CFL official {label} response is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("status") != 200:
        raise ValueError(f"CFL official {label} response status is invalid")
    return value


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"CFL official {field} is missing")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _venue_record(
    venue_name: object,
    venue_name_en: object,
    *,
    fixture_source: dict[str, Any],
) -> dict[str, Any]:
    """Build one fail-closed venue row with field-level city provenance."""

    name = _optional_text(venue_name)
    name_en = _optional_text(venue_name_en)
    venue: dict[str, Any] = {"name": name, "name_en": name_en}
    if name is None or name_en is None:
        return venue

    key = (name, name_en)
    city = _EXPLICIT_VENUE_CITY_BY_OFFICIAL_LABEL.get(key)
    if city is not None:
        venue.update(
            {
                "city": city,
                "country": "China",
                "country_code": "CN",
                "city_resolution": {
                    "basis": "official_venue_label_explicit_city",
                    "matched_on": {"name": name, "name_en": name_en},
                    "source": {
                        "name": fixture_source.get("name"),
                        "source_id": fixture_source.get("source_id"),
                        "url": fixture_source.get("url"),
                        "retrieved_at": fixture_source.get("retrieved_at"),
                        "raw_sha256": fixture_source.get("raw_sha256"),
                        "rights_status": fixture_source.get("rights_status"),
                        "commercial_reuse_verified": fixture_source.get(
                            "commercial_reuse_verified"
                        ),
                    },
                },
            }
        )
        return venue

    curated = _CURATED_VENUE_CITY_BY_OFFICIAL_LABEL.get(key)
    if curated is None:
        return venue
    venue.update(
        {
            "city": curated["city"],
            "country": "China",
            "country_code": "CN",
            "city_resolution": {
                "basis": "first_party_static_venue_city_registry_v1",
                "matched_on": {"name": name, "name_en": name_en},
                "source": {
                    field: curated[field]
                    for field in (
                        "name",
                        "url",
                        "raw_sha256",
                        "reviewed_on",
                        "rights_status",
                        "commercial_reuse_verified",
                    )
                },
            },
        }
    )
    return venue


def validate_cfl_venue(venue: object, *, fixture_source: dict[str, Any]) -> None:
    """Reject invented or altered city metadata in a persisted CFL row."""

    if not isinstance(venue, dict):
        raise ValueError("CFL official fixture venue is invalid")
    expected = _venue_record(
        venue.get("name"), venue.get("name_en"), fixture_source=fixture_source
    )
    if venue != expected:
        raise ValueError("CFL official fixture venue city provenance is invalid")


def _score(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"CFL official {field} is invalid")
    return value


def _local_kickoff(item: dict[str, Any], *, index: int) -> tuple[str, str]:
    raw = _required_text(item.get("local_date_time"), field=f"dataList[{index}].local_date_time")
    try:
        naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("CFL official kickoff time is invalid") from exc
    source_date = _required_text(item.get("local_date"), field=f"dataList[{index}].local_date")
    if source_date != naive.date().isoformat():
        raise ValueError("CFL official kickoff date conflicts with local_date_time")
    source_time = item.get("local_time")
    if (
        isinstance(source_time, str)
        and source_time.strip()
        and source_time.strip() != naive.time().isoformat()
    ):
        raise ValueError("CFL official kickoff time conflicts with local_date_time")
    local = naive.replace(tzinfo=_LOCAL_TIMEZONE)
    return source_date, local.astimezone(timezone.utc).isoformat()


def _fact_times(item: dict[str, Any], *, observed: datetime) -> tuple[str, dict[str, str | None]]:
    raw = item.get("updateTime")
    fallback_reason: str | None = None
    try:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError
        local = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_LOCAL_TIMEZONE)
        effective = local.astimezone(timezone.utc)
        if effective > observed + timedelta(minutes=5):
            fallback_reason = "provider_update_time_after_observation"
            effective = observed
    except ValueError:
        fallback_reason = "provider_update_time_missing_or_invalid"
        effective = observed
    return effective.isoformat(), {
        "effective_at_basis": (
            "provider_update_time" if fallback_reason is None else "capture_retrieved_at"
        ),
        "observed_at_basis": "capture_retrieved_at",
        "fallback_reason": fallback_reason,
    }


def _matches_query(url: str) -> dict[str, list[str]]:
    _validate_api_url(url, expected_path=MATCHES_PATH)
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    allowed = {
        "tournament_calendar_id",
        "competition_code",
        "contestant_id",
        "week",
        "stage_id",
        "curPage",
        "pageSize",
    }
    if not set(query).issubset(allowed) or any(len(values) != 1 for values in query.values()):
        raise ValueError("CFL official matches query is not allowlisted")
    return query


def parse_cfl_matches(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
    tournament_calendar_id: str,
    stage_id: str,
    season: str = "2026",
) -> list[dict[str, Any]]:
    """Parse one complete, allowlisted CSL season response."""

    observed = _utc(retrieved_at, field="retrieved_at")
    tournament_id = _required_text(tournament_calendar_id, field="tournament_calendar_id")
    selected_stage_id = _required_text(stage_id, field="stage_id")
    selected_season = _required_text(season, field="season")
    query = _matches_query(url)
    required_query = {
        "tournament_calendar_id": tournament_id,
        "competition_code": "CSL",
        "stage_id": selected_stage_id,
        "curPage": "1",
        "pageSize": str(MAX_SEASON_MATCHES),
    }
    if any(query.get(key) != [value] for key, value in required_query.items()):
        raise ValueError("CFL official matches query does not match parser scope")

    root = _json_object(payload, label="matches")
    data = root.get("data")
    if not isinstance(data, dict) or data.get("status") != 0:
        raise ValueError("CFL official matches data envelope is invalid")
    rows = data.get("dataList")
    count = data.get("count")
    if (
        not isinstance(rows, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(rows)
        or count > MAX_SEASON_MATCHES
        or any(not isinstance(item, dict) for item in rows)
    ):
        raise ValueError("CFL official matches list is incomplete or oversized")

    digest = hashlib.sha256(payload).hexdigest()
    source_id = f"cfl-official:csl:{selected_season}"
    source: dict[str, Any] = {
        "name": SOURCE_NAME,
        "source_id": source_id,
        "url": url,
        "official_page_url": CFL_OFFICIAL_PAGE_URL,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": digest,
        "hash": f"sha256:{digest}",
        "license": SOURCE_LICENSE,
        "rights_status": SOURCE_RIGHTS_STATUS,
        "commercial_reuse_verified": False,
        "access_basis": SOURCE_ACCESS_BASIS,
    }
    parsed: list[dict[str, Any]] = []
    native_ids: set[str] = set()
    status_map = {
        "Played": "finished",
        "Fixture": "upcoming",
        "Postponed": "postponed",
        "Cancelled": "cancelled",
    }
    for index, item in enumerate(rows):
        native_id = _required_text(item.get("id"), field=f"dataList[{index}].id")
        if not _NATIVE_ID_RE.fullmatch(native_id):
            raise ValueError("CFL official fixture ID is invalid")
        if native_id in native_ids:
            raise ValueError("CFL official response contains a duplicate official fixture ID")
        native_ids.add(native_id)
        if item.get("competition_code") != "CSL" or item.get("competition_id") in (None, ""):
            raise ValueError("CFL official fixture competition does not match CSL")
        if item.get("tournament_calendar_id") != tournament_id:
            raise ValueError("CFL official fixture tournament does not match requested season")
        if str(item.get("tournament_calendar_name") or "") != selected_season:
            raise ValueError("CFL official fixture season does not match requested season")
        if item.get("stage_id") != selected_stage_id:
            raise ValueError("CFL official fixture stage does not match requested stage")

        match_status = _required_text(
            item.get("match_status"), field=f"dataList[{index}].match_status"
        )
        if match_status not in status_map:
            raise ValueError("CFL official fixture status is not allowlisted")
        home_provider_id = _required_text(
            item.get("home_contestant_id"),
            field=f"dataList[{index}].home_contestant_id",
        )
        away_provider_id = _required_text(
            item.get("away_contestant_id"),
            field=f"dataList[{index}].away_contestant_id",
        )
        home = _required_text(
            item.get("home_contestant_name"),
            field=f"dataList[{index}].home_contestant_name",
        )
        away = _required_text(
            item.get("away_contestant_name"),
            field=f"dataList[{index}].away_contestant_name",
        )
        if home_provider_id == away_provider_id or home == away:
            raise ValueError("CFL official fixture home and away identities conflict")
        kickoff_date, kickoff_at = _local_kickoff(item, index=index)
        effective_at, time_semantics = _fact_times(item, observed=observed)
        week = item.get("week")
        if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 60:
            raise ValueError("CFL official fixture week is invalid")

        score: dict[str, int] | None = None
        halftime: dict[str, int] | None = None
        if match_status == "Played":
            score = {
                "home": _score(item.get("ft_home_score"), field="fulltime home score"),
                "away": _score(item.get("ft_away_score"), field="fulltime away score"),
            }
            halftime = {
                "home": _score(item.get("ht_home_score"), field="halftime home score"),
                "away": _score(item.get("ht_away_score"), field="halftime away score"),
            }
            if halftime["home"] > score["home"] or halftime["away"] > score["away"]:
                raise ValueError("CFL official halftime score exceeds the fulltime score")

        home_en = item.get("home_contestant_name_en")
        away_en = item.get("away_contestant_name_en")
        venue_name = item.get("venue_long_name") or item.get("venue_short_name")
        venue_name_en = item.get("venue_long_name_en") or item.get("venue_short_name_en")
        lineage = {
            "source_id": source_id,
            "url": url,
            "retrieved_at": observed.isoformat(),
            "raw_sha256": digest,
            "hash": f"sha256:{digest}",
            "license": SOURCE_LICENSE,
            "rights_status": SOURCE_RIGHTS_STATUS,
            "parser": "cfl_official_matches_v1",
            "record_index": index,
            "record_path": f"$.data.dataList[{index}]",
            "source_row_id": f"{source_id}#$.data.dataList[{index}]",
            "native_fixture_id": native_id,
            "provider_updated_at_raw": item.get("updateTime"),
            "effective_at": effective_at,
            "observed_at": observed.isoformat(),
        }
        record: dict[str, Any] = {
            "id": f"cfl-official:csl:{native_id}",
            "native_fixture_id": native_id,
            "provider_fixture_ids": {"cfl_official": native_id},
            "competition_id": "csl",
            "provider_competition_id": str(item["competition_id"]),
            "season": selected_season,
            "round": str(week),
            "kickoff_date": kickoff_date,
            "kickoff_at": kickoff_at,
            "kickoff_time_quality": "exact",
            "kickoff_time_source": "official_local_date_time",
            "kickoff_timezone": "Asia/Shanghai",
            "effective_at": effective_at,
            "observed_at": observed.isoformat(),
            "time_semantics": time_semantics,
            "home_team": home,
            "away_team": away,
            "home_team_en": home_en.strip()
            if isinstance(home_en, str) and home_en.strip()
            else None,
            "away_team_en": away_en.strip()
            if isinstance(away_en, str) and away_en.strip()
            else None,
            "home_provider_team_id": home_provider_id,
            "away_provider_team_id": away_provider_id,
            "provider_team_ids": {
                "home": {"cfl_official": home_provider_id},
                "away": {"cfl_official": away_provider_id},
            },
            "status": status_map[match_status],
            "score": score,
            "halftime_score": halftime,
            "result_scope": "regulation_90" if score is not None else None,
            "venue": _venue_record(
                venue_name,
                venue_name_en,
                fixture_source=source,
            ),
            "source": dict(source),
            "lineage": lineage,
        }
        parsed.append(record)
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("CFL official API redirected; redirects are disabled")


def _safe_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _read_source(
    url: str,
    *,
    expected_path: str,
    opener: Callable[..., object] | object | None,
) -> bytes:
    _validate_api_url(url, expected_path=expected_path)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    client = opener if opener is not None else _safe_opener()
    open_method = getattr(client, "open", None) or client
    payload: bytes | None = None
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            response = open_method(  # type: ignore[operator]
                request, timeout=DEFAULT_TIMEOUT_SECONDS
            )
            with response:
                final_url = str(getattr(response, "geturl", lambda: request.full_url)())
                if final_url != request.full_url:
                    raise ValueError("CFL official API redirected; redirects are disabled")
                _validate_api_url(final_url, expected_path=expected_path)
                payload = read_response_bounded(
                    response,
                    MAX_CONTENT_BYTES,
                    timeout_seconds=RESPONSE_READ_TIMEOUT_SECONDS,
                )
            break
        except urllib.error.HTTPError:
            # An HTTP policy or application error is not a transient transport
            # failure. Retrying it would only add load and could disguise an
            # access-control boundary.
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 >= MAX_REQUEST_ATTEMPTS:
                raise
    if payload is None:  # defensive: the bounded loop either returns or raises
        raise RuntimeError("CFL official response was not read")
    _validate_payload(payload)
    return payload


def _api_url(path: str, query: list[tuple[str, str]]) -> str:
    if path not in ALLOWED_PATHS:
        raise ValueError("CFL official API path is not allowlisted")
    return f"{CFL_API_BASE_URL}{path}?{urlencode(query)}"


def _discover_tournament(payload: bytes, *, season: str) -> dict[str, Any]:
    root = _json_object(payload, label="tournaments")
    data = root.get("data")
    rows = data.get("dataList") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("CFL official tournaments list is invalid")
    candidates = [
        row
        for row in rows
        if row.get("competition_code") == "CSL"
        and str(row.get("name") or "") == season
        and row.get("active") == "yes"
        and row.get("valid") == "yes"
        and row.get("id")
        and row.get("competition_id")
    ]
    if len(candidates) != 1:
        raise ValueError("CFL official current tournament is missing or ambiguous")
    return candidates[0]


def _discover_stage(payload: bytes) -> dict[str, Any]:
    root = _json_object(payload, label="stages")
    rows = root.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("CFL official stages list is invalid")
    regular = [
        row
        for row in rows
        if row.get("stage_id")
        and (
            row.get("stage_name_en") == "Regular Season"
            or (len(rows) == 1 and row.get("stage_name"))
        )
    ]
    if len(regular) != 1:
        raise ValueError("CFL official regular-season stage is missing or ambiguous")
    return regular[0]


def _kickoff_value(fixture: dict[str, Any]) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(fixture["kickoff_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CFL official fixture kickoff_at is invalid") from exc
    return _utc(parsed, field="kickoff_at")


def _empty_result(reference: datetime, *, errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": SOURCE_NAME,
        "retrieved_at": reference.isoformat(),
        "season": str(reference.astimezone(_LOCAL_TIMEZONE).year),
        "tournament_calendar_id": None,
        "stage_id": None,
        "fixtures": [],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [],
        "errors": errors,
        "status": "unavailable",
        "source_contract": _source_contract(),
        "diagnostics": {
            "parsed_fixture_count": 0,
            "finished_fixture_count": 0,
            "upcoming_fixture_count": 0,
            "postponed_fixture_count": 0,
        },
    }


def _source_contract() -> dict[str, Any]:
    return {
        "fact_source": SOURCE_NAME,
        "transport_role": "HTTPS fetch execution only; Crawl4AI, if used, is never the fact source",
        "official_page_url": CFL_OFFICIAL_PAGE_URL,
        "api_host": CFL_API_HOST,
        "allowed_paths": sorted(ALLOWED_PATHS),
        "license": SOURCE_LICENSE,
        "rights_status": SOURCE_RIGHTS_STATUS,
        "commercial_reuse_verified": False,
        "access_basis": SOURCE_ACCESS_BASIS,
        "redirects": "disabled",
        "max_response_bytes": MAX_CONTENT_BYTES,
        "page_size": MAX_SEASON_MATCHES,
        "row_lineage": "source_id+url+retrieved_at+sha256+rights_status+official IDs+record_pointer",
    }


def fetch_cfl_current(
    *,
    now: datetime | None = None,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Return the central pre-network block for the official public interface."""

    return rights_blocked_fetch_envelope(
        SourceId.CFL_OFFICIAL_CURRENT,
        provider=SOURCE_NAME,
        now=now,
        empty_fields=(
            "fixtures",
            "date_only_fixtures",
            "recent_results",
            "upcoming_3_days",
            "upcoming_7_days",
        ),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc), field="now")
    season = str(reference.astimezone(_LOCAL_TIMEZONE).year)
    try:
        tournaments_url = _api_url(TOURNAMENTS_PATH, [("competition_code", "CSL")])
        tournaments_payload = _read_source(
            tournaments_url, expected_path=TOURNAMENTS_PATH, opener=opener
        )
        tournament = _discover_tournament(tournaments_payload, season=season)
        tournament_id = str(tournament["id"])

        stage_url = _api_url(
            STAGE_PATH,
            [
                ("tournament_calendar_id", tournament_id),
                ("competition_code", "CSL"),
            ],
        )
        stage_payload = _read_source(stage_url, expected_path=STAGE_PATH, opener=opener)
        stage = _discover_stage(stage_payload)
        stage_id = str(stage["stage_id"])

        matches_url = _api_url(
            MATCHES_PATH,
            [
                ("tournament_calendar_id", tournament_id),
                ("competition_code", "CSL"),
                ("contestant_id", ""),
                ("week", ""),
                ("stage_id", stage_id),
                ("curPage", "1"),
                ("pageSize", str(MAX_SEASON_MATCHES)),
            ],
        )
        matches_payload = _read_source(matches_url, expected_path=MATCHES_PATH, opener=opener)
        fixtures = parse_cfl_matches(
            matches_payload,
            retrieved_at=reference,
            url=matches_url,
            tournament_calendar_id=tournament_id,
            stage_id=stage_id,
            season=season,
        )
    except Exception as exc:  # fail closed for the complete discovery chain
        return _empty_result(
            reference,
            errors=[{"stage": "discover_or_fetch", "error": str(exc)}],
        )

    fixtures.sort(key=lambda row: (_kickoff_value(row), row["id"]))
    recent_start = reference - timedelta(days=7)
    upcoming_3_end = reference + timedelta(days=3)
    upcoming_7_end = reference + timedelta(days=7)
    recent_results = [
        row
        for row in fixtures
        if row["status"] == "finished" and recent_start <= _kickoff_value(row) <= reference
    ]
    upcoming_3_days = [
        row
        for row in fixtures
        if row["status"] == "upcoming" and reference <= _kickoff_value(row) <= upcoming_3_end
    ]
    upcoming_7_days = [
        row
        for row in fixtures
        if row["status"] == "upcoming" and reference <= _kickoff_value(row) <= upcoming_7_end
    ]
    return {
        "provider": SOURCE_NAME,
        "retrieved_at": reference.isoformat(),
        "season": season,
        "tournament_calendar_id": tournament_id,
        "stage_id": stage_id,
        "fixtures": fixtures,
        "date_only_fixtures": [],
        "recent_results": recent_results,
        "upcoming_3_days": upcoming_3_days,
        "upcoming_7_days": upcoming_7_days,
        "errors": [],
        "status": "ok" if fixtures else "unavailable",
        "source_contract": _source_contract(),
        "discovery_lineage": {
            "tournaments": {
                "url": tournaments_url,
                "raw_sha256": hashlib.sha256(tournaments_payload).hexdigest(),
            },
            "stage": {
                "url": stage_url,
                "raw_sha256": hashlib.sha256(stage_payload).hexdigest(),
            },
            "matches": {
                "url": matches_url,
                "raw_sha256": hashlib.sha256(matches_payload).hexdigest(),
            },
            "retrieved_at": reference.isoformat(),
        },
        "diagnostics": {
            "parsed_fixture_count": len(fixtures),
            "finished_fixture_count": sum(row["status"] == "finished" for row in fixtures),
            "upcoming_fixture_count": sum(row["status"] == "upcoming" for row in fixtures),
            "postponed_fixture_count": sum(row["status"] == "postponed" for row in fixtures),
        },
    }


__all__ = [
    "ALLOWED_PATHS",
    "CFL_API_HOST",
    "CFL_OFFICIAL_PAGE_URL",
    "MAX_CONTENT_BYTES",
    "SOURCE_NAME",
    "SOURCE_RIGHTS_STATUS",
    "fetch_cfl_current",
    "parse_cfl_matches",
    "validate_cfl_venue",
]
