"""Historical Sports Lottery result and odds-history adapter.

The official gateway exposes historical match results and a per-pool odds
change list.  The odds change rows carry ``updateDate`` and ``updateTime``,
but the fixed-bonus response does not carry a kickoff timestamp.  Therefore
this module preserves the timestamps and keeps rows out of the model until a
separate, exact kickoff timestamp proves that the update was pre-match.

Only the public HTTPS gateway is used.  Redirects, oversized responses and
non-allowlisted hosts are rejected; callers may inject an opener for tests.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


SPORTS_LOTTERY_HISTORY_HOST = "webapi.sporttery.cn"
SPORTS_LOTTERY_RESULT_PATH = "/gateway/uniform/football/getUniformMatchResultV1.qry"
SPORTS_LOTTERY_FIXED_BONUS_PATH = "/gateway/uniform/football/getFixedBonusV1.qry"
SPORTS_LOTTERY_HISTORY_URL = f"https://{SPORTS_LOTTERY_HISTORY_HOST}{SPORTS_LOTTERY_RESULT_PATH}"
SPORTS_LOTTERY_FIXED_BONUS_URL = (
    f"https://{SPORTS_LOTTERY_HISTORY_HOST}{SPORTS_LOTTERY_FIXED_BONUS_PATH}"
)
SPORTS_LOTTERY_HISTORY_ALLOWED_PATHS = {
    SPORTS_LOTTERY_RESULT_PATH,
    SPORTS_LOTTERY_FIXED_BONUS_PATH,
}
MAX_CONTENT_BYTES = 10 * 1024 * 1024
_CHINA_TZ = ZoneInfo("Asia/Shanghai")
_POOL_KEYS = {
    "hadList": ("had", ("h", "d", "a")),
    "hhadList": ("hhad", ("h", "d", "a")),
    "ttgList": ("ttg", tuple(f"s{i}" for i in range(8))),
    "hafuList": ("hafu", ("hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa")),
    "crsList": ("crs", ()),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: D401
        raise ValueError("Sports Lottery historical endpoint redirected; refusing to follow it")


def _reference_time(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _validate_url(url: str, path: str | None = None) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SPORTS_LOTTERY_HISTORY_HOST
        or parsed.path not in SPORTS_LOTTERY_HISTORY_ALLOWED_PATHS
        or (path is not None and parsed.path != path)
    ):
        raise ValueError("Sports Lottery historical URL is not allowlisted")


def _read_limited(opener: Any, request: urllib.request.Request) -> tuple[bytes, str]:
    with opener.open(request, timeout=30) as response:  # fixed allowlisted host
        final_url = response.geturl()
        _validate_url(final_url)
        payload = response.read(MAX_CONTENT_BYTES + 1)
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("Sports Lottery historical response exceeded 10 MiB")
    return payload, final_url


def _read_with_retry(
    opener: Any,
    request: urllib.request.Request,
    *,
    attempts: int = 2,
) -> tuple[bytes, str]:
    """Retry only transient public-endpoint failures.

    A retry must never turn an access-control response or an invalid redirect
    into success.  Only HTTP 429/5xx and transient transport failures are retried, with
    a small bounded backoff; the final error remains source-isolated.
    """

    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return _read_limited(opener, request)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _source(*, url: str, payload: bytes, retrieved_at: datetime) -> dict[str, Any]:
    return {
        "name": "Sports Lottery official historical gateway",
        "url": url,
        "source_level": "official",
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _json_payload(payload: bytes) -> Mapping[str, Any]:
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("Sports Lottery historical response exceeded 10 MiB")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Sports Lottery historical response is not JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("Sports Lottery historical response root must be an object")
    error_code = value.get("errorCode")
    if error_code is not None and str(error_code) not in {"0", ""}:
        message = str(value.get("errorMessage") or "unknown gateway error")
        raise ValueError(f"Sports Lottery historical gateway error {error_code}: {message}")
    return value


def _odds(values: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in keys:
        try:
            value = float(values.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            result[key] = value
    return result


def _scoreline_odds(values: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, raw in values.items():
        if not isinstance(key, str) or not key.startswith("s") or key.endswith("f"):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            result[key] = value
    return result


def _devig(values: Mapping[str, float]) -> dict[str, float]:
    inverse = {key: 1.0 / value for key, value in values.items() if value > 0}
    total = sum(inverse.values())
    if not inverse or not math.isfinite(total) or total <= 0:
        return {}
    return {key: round(value / total, 8) for key, value in inverse.items()}


def _effective_timestamp(row: Mapping[str, Any], *, observed_at: datetime) -> tuple[str, str]:
    update_date = str(row.get("updateDate") or "").strip()
    update_time = str(row.get("updateTime") or "").strip()
    if update_date and update_time:
        try:
            parsed = datetime.strptime(
                f"{update_date} {update_time}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=_CHINA_TZ)
        except ValueError as exc:
            raise ValueError("Sports Lottery update timestamp is invalid") from exc
        effective = parsed.astimezone(timezone.utc)
        if effective > observed_at:
            raise ValueError("Sports Lottery update timestamp is in the future")
        return effective.isoformat(), "provider_update_datetime"
    return observed_at.isoformat(), "observed_at_fallback_no_provider_timestamp"


def parse_uniform_match_result(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = SPORTS_LOTTERY_HISTORY_URL,
) -> list[dict[str, Any]]:
    """Parse historical results without inventing kickoff times."""

    _validate_url(url, SPORTS_LOTTERY_RESULT_PATH)
    observed_at = _reference_time(retrieved_at)
    data = _json_payload(payload)
    value = data.get("value")
    rows = value.get("matchResult", []) if isinstance(value, Mapping) else []
    if not isinstance(rows, list):
        raise ValueError("Sports Lottery result payload has no matchResult list")
    source = _source(url=url, payload=payload, retrieved_at=observed_at)
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("matchId") is None:
            continue
        result.append(
            {
                "match_id": str(row["matchId"]),
                "match_num": str(row.get("matchNumStr") or row.get("matchNum") or ""),
                "league": str(row.get("leagueName") or ""),
                "home_team": str(row.get("allHomeTeam") or row.get("homeTeam") or ""),
                "away_team": str(row.get("allAwayTeam") or row.get("awayTeam") or ""),
                "match_date": str(row.get("matchDate") or ""),
                "kickoff_at": None,
                "kickoff_time_quality": "date_only",
                "home_score": _score_part(row.get("sectionsNo999"), 0),
                "away_score": _score_part(row.get("sectionsNo999"), 1),
                "goal_line": str(row.get("goalLine") or ""),
                "had_odds": _odds(row, ("h", "d", "a")),
                "source": source,
                "enters_model": False,
                "model_exclusion_reason": "official_result_endpoint_has_no_exact_kickoff_time",
            }
        )
    return result


def _score_part(value: Any, index: int) -> int | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    parts = value.split(":", 1)
    try:
        score = int(parts[index])
    except (IndexError, TypeError, ValueError):
        return None
    return score if score >= 0 else None


def parse_fixed_bonus_history(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = SPORTS_LOTTERY_FIXED_BONUS_URL,
    kickoff_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Parse timestamped pool changes, conservatively gating model use.

    ``kickoff_at`` is optional by design.  Without an independently verified
    exact kickoff, every row is audit-only even when the provider supplied an
    update timestamp.
    """

    _validate_url(url, SPORTS_LOTTERY_FIXED_BONUS_PATH)
    observed_at = _reference_time(retrieved_at)
    data = _json_payload(payload)
    value = data.get("value")
    history = value.get("oddsHistory") if isinstance(value, Mapping) else None
    if not isinstance(history, Mapping):
        return []
    source = _source(url=url, payload=payload, retrieved_at=observed_at)
    match_id = str(history.get("matchId") or "")
    result: list[dict[str, Any]] = []
    for list_key, (pool, probability_keys) in _POOL_KEYS.items():
        rows = history.get(list_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            effective_at, effective_source = _effective_timestamp(row, observed_at=observed_at)
            odds = _odds(row, probability_keys)
            if pool == "crs":
                odds = _scoreline_odds(row)
            effective_dt = datetime.fromisoformat(effective_at)
            kickoff_utc = (
                kickoff_at.astimezone(timezone.utc)
                if kickoff_at is not None
                and kickoff_at.tzinfo is not None
                and kickoff_at.utcoffset() is not None
                else None
            )
            # A provider timestamp describes when the odds changed, but it
            # does not prove that Matchline had observed that row before the
            # match.  A history endpoint queried after kickoff must remain
            # audit-only even when every provider update is pre-match; using
            # it as a feature would introduce a retrospective observation
            # leak.
            model_eligible = bool(
                kickoff_utc is not None
                and observed_at < kickoff_utc
                and effective_source == "provider_update_datetime"
                and effective_dt < kickoff_utc
            )
            if model_eligible:
                exclusion_reason = None
            elif kickoff_utc is None:
                exclusion_reason = "exact_pre_match_kickoff_not_proven"
            elif observed_at >= kickoff_utc:
                exclusion_reason = "first_observation_not_pre_match"
            elif effective_source != "provider_update_datetime":
                exclusion_reason = "provider_update_timestamp_not_proven"
            elif effective_dt >= kickoff_utc:
                exclusion_reason = "provider_timestamp_not_pre_match"
            else:  # pragma: no cover - defensive fallback for future policy changes.
                exclusion_reason = "exact_pre_match_kickoff_not_proven"
            result.append(
                {
                    "match_id": match_id,
                    "pool": pool,
                    "goal_line": str(row.get("goalLine") or ""),
                    "odds": odds,
                    "probability": _devig(odds),
                    "effective_at": effective_at,
                    "effective_at_source": effective_source,
                    "observed_at": observed_at.isoformat(),
                    "enters_model": model_eligible,
                    "model_exclusion_reason": exclusion_reason,
                    "source": source,
                }
            )
    return result


def _opener(opener: Any | None) -> Any:
    return opener or urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def fetch_uniform_match_result(
    match_date: date | str,
    *,
    now: datetime | None = None,
    opener: Any | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Fetch one calendar date from the official historical result endpoint."""

    return rights_blocked_fetch_envelope(
        SourceId.SPORTS_LOTTERY_OFFICIAL,
        provider="Sports Lottery official historical gateway",
        now=now,
        empty_fields=("matches",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    date_text = match_date.isoformat() if isinstance(match_date, date) else str(match_date)
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("match_date must be YYYY-MM-DD") from exc
    query = urllib.parse.urlencode(
        {
            "matchBeginDate": date_text,
            "matchEndDate": date_text,
            "pageSize": 100,
            "pageNo": 1,
            "isFix": 0,
            "matchPage": 1,
            "pcOrWap": 1,
        }
    )
    url = f"{SPORTS_LOTTERY_HISTORY_URL}?{query}"
    observed_at = _reference_time(now)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.sporttery.cn/",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    payload, final_url = _read_with_retry(_opener(opener), request)
    return {
        "provider": "Sports Lottery official historical gateway",
        "retrieved_at": observed_at.isoformat(),
        "request_url": final_url,
        "matches": parse_uniform_match_result(payload, retrieved_at=observed_at, url=final_url),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "errors": [],
    }


def fetch_fixed_bonus_history(
    match_id: str | int,
    *,
    now: datetime | None = None,
    kickoff_at: datetime | None = None,
    opener: Any | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Fetch one match's official odds-history record."""

    return rights_blocked_fetch_envelope(
        SourceId.SPORTS_LOTTERY_OFFICIAL,
        provider="Sports Lottery official historical gateway",
        now=now,
        empty_fields=("odds_history",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    match_text = str(match_id).strip()
    if not match_text or not match_text.isdigit():
        raise ValueError("match_id must be a numeric Sports Lottery id")
    url = f"{SPORTS_LOTTERY_FIXED_BONUS_URL}?{urllib.parse.urlencode({'clientCode': 3001, 'matchId': match_text})}"
    observed_at = _reference_time(now)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.sporttery.cn/",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    payload, final_url = _read_with_retry(_opener(opener), request)
    return {
        "provider": "Sports Lottery official historical gateway",
        "retrieved_at": observed_at.isoformat(),
        "request_url": final_url,
        "match_id": match_text,
        "odds_history": parse_fixed_bonus_history(
            payload,
            retrieved_at=observed_at,
            url=final_url,
            kickoff_at=kickoff_at,
        ),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "errors": [],
    }


__all__ = [
    "SPORTS_LOTTERY_FIXED_BONUS_URL",
    "SPORTS_LOTTERY_HISTORY_URL",
    "fetch_fixed_bonus_history",
    "fetch_uniform_match_result",
    "parse_fixed_bonus_history",
    "parse_uniform_match_result",
]
