"""Current fixtures/results adapter for ESPN's public scoreboard feed."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


ESPN_CODES = {
    "premier-league": "eng.1",
    "championship": "eng.2",
    "la-liga": "esp.1",
    "bundesliga": "ger.1",
    "serie-a": "ita.1",
    "ligue-1": "fra.1",
    "csl": "chn.1",
}
ESPN_HOST = "site.api.espn.com"
ESPN_ALLOWED_HOSTS = {"site.api.espn.com", "site.web.api.espn.com"}
ESPN_PATH_PREFIX = "/apis/site/v2/"
ESPN_TERMS_URL = "https://disneytermsofuse.com/english/"
ESPN_RIGHTS_STATUS = "blocked_pending_express_written_permission"
STATUS_MAP = {
    "STATUS_SCHEDULED": "upcoming",
    "STATUS_IN_PROGRESS": "live",
    "STATUS_FIRST_HALF": "live",
    "STATUS_SECOND_HALF": "live",
    "STATUS_HALFTIME": "live",
    "STATUS_FULL_TIME": "finished",
    "STATUS_FINAL_AET": "finished",
    "STATUS_FINAL_PEN": "finished",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "cancelled",
}
RESULT_SCOPE_MAP = {
    "STATUS_FULL_TIME": "regulation_90",
    "STATUS_FINAL_AET": "extra_time",
    "STATUS_FINAL_PEN": "penalties",
}


def _espn_rights_block(*, provider: str, checked_at: datetime) -> dict:
    """Return the shared fail-closed policy envelope before any I/O."""

    return {
        "provider": provider,
        "retrieved_at": None,
        "checked_at": checked_at.astimezone(timezone.utc).isoformat(),
        "status": "rights_blocked",
        "errors": [],
        "access_allowed": False,
        "network_opened": False,
        "rights_status": ESPN_RIGHTS_STATUS,
        "authorization_required": "express_written_permission",
        "terms_url": ESPN_TERMS_URL,
        "model_eligible": False,
        "enters_model": False,
        "model_exclusion_reason": "provider_rights_blocked",
    }


def _validate_response_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ESPN_ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith(ESPN_PATH_PREFIX)
    ):
        raise ValueError("ESPN response redirected to a non-allowlisted URL")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("ESPN redirected; redirects are disabled")


def _safe_opener():
    return urllib.request.build_opener(_NoRedirect())


def _read_limited(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    max_bytes: int,
) -> bytes:
    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=30)  # type: ignore[operator]
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_response_url(str(final_url))
        payload = response.read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise TypeError("ESPN response must be bytes")
    if len(payload) > max_bytes:
        raise ValueError("ESPN response exceeded 10 MiB")
    return payload


def _venue_record(competition: dict) -> dict | None:
    venue = competition.get("venue")
    if not isinstance(venue, dict):
        return None
    address = venue.get("address") if isinstance(venue.get("address"), dict) else {}
    record = {
        "provider_venue_id": str(venue["id"]) if venue.get("id") is not None else None,
        "name": venue.get("fullName") or venue.get("name"),
        "city": address.get("city") or venue.get("city"),
        "country": address.get("country") or venue.get("country"),
        "country_code": address.get("countryCode") or venue.get("countryCode"),
    }
    coordinates = venue.get("coordinates") or venue.get("geo")
    if isinstance(coordinates, dict):
        latitude = coordinates.get("latitude", coordinates.get("lat"))
        longitude = coordinates.get("longitude", coordinates.get("lon"))
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            latitude = longitude = None
        if (
            latitude is not None
            and longitude is not None
            and math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            record["latitude"] = latitude
            record["longitude"] = longitude
    cleaned = {key: value for key, value in record.items() if value not in (None, "")}
    return cleaned or None


def parse_espn_payload(
    payload: bytes, *, competition_id: str, retrieved_at: datetime, url: str
) -> list[dict]:
    data = json.loads(payload)
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    fixtures = []
    for event in data.get("events", []):
        competition = event["competitions"][0]
        competitors = {item["homeAway"]: item for item in competition["competitors"]}
        if set(competitors) != {"home", "away"}:
            continue
        home = competitors["home"]
        away = competitors["away"]
        status_name = event["status"]["type"]["name"]
        status = STATUS_MAP.get(status_name, "upcoming")
        score = None
        if status == "finished":
            score = {
                "home": int(float(home.get("score", 0))),
                "away": int(float(away.get("score", 0))),
            }
        fixtures.append(
            {
                "id": f"espn:{event['id']}",
                "competition_id": competition_id,
                "season": str(event.get("season", {}).get("year") or event["date"][:4]),
                "kickoff_at": event["date"].replace("Z", "+00:00"),
                "home_team": home["team"]["displayName"],
                "away_team": away["team"]["displayName"],
                "home_provider_team_id": str(home["team"]["id"]),
                "away_provider_team_id": str(away["team"]["id"]),
                "venue": _venue_record(competition),
                "status": status,
                # Keep the settlement scope explicit.  A finished cup match
                # can be decided in extra time or penalties; those scores are
                # display-only for Matchline's 90-minute result contract.
                "result_scope": RESULT_SCOPE_MAP.get(status_name),
                "score": score,
                "source": {
                    "name": "ESPN",
                    "url": url,
                    "native_fixture_id": str(event["id"]),
                    "retrieved_at": retrieved_at.isoformat(),
                    "raw_sha256": raw_sha256,
                },
            }
        )
    return fixtures


def fetch_espn_fixtures(
    *,
    now: datetime | None = None,
    horizon_days: int = 45,
    lookback_days: int = 45,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: str | None = None,
) -> dict:
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    return {
        **rights_blocked_envelope(
            SourceId.ESPN_SCHEDULE_SUMMARY,
            provider="ESPN",
            checked_at=reference_time.astimezone(timezone.utc).isoformat(),
            empty_fields=("fixtures",),
            authorization_reference=authorization_reference,
        ),
        "authorization_required": "express_written_permission",
        "terms_url": ESPN_TERMS_URL,
        "enters_model": False,
        "model_exclusion_reason": "provider_rights_blocked",
        "horizon_days": horizon_days,
        "lookback_days": lookback_days,
    }
    # Retained below as a parser/reference implementation only.  Policy v260
    # deliberately returns above before constructing an opener or request.
    authorization_reference = (
        authorization_reference.strip()
        if isinstance(authorization_reference, str) and authorization_reference.strip()
        else None
    )
    if authorization_reference is None:
        return {
            **_espn_rights_block(provider="ESPN", checked_at=reference_time),
            "horizon_days": horizon_days,
            "lookback_days": lookback_days,
            "fixtures": [],
        }
    start = reference_time - timedelta(days=lookback_days)
    end = reference_time + timedelta(days=horizon_days)
    date_range = f"{start:%Y%m%d}-{end:%Y%m%d}"
    fixtures = []
    errors = []
    observation_times = []
    fetcher = opener if opener is not None else _safe_opener()
    for competition_id, code in ESPN_CODES.items():
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/soccer/"
            f"{code}/scoreboard?dates={date_range}&limit=500"
        )
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS ESPN host
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
        )
        try:
            payload = _read_limited(fetcher, request, max_bytes=10 * 1024 * 1024)
            observed_at = reference_time if now is not None else datetime.now(timezone.utc)
            observation_times.append(observed_at)
            fixtures.extend(
                parse_espn_payload(
                    payload,
                    competition_id=competition_id,
                    retrieved_at=observed_at,
                    url=url,
                )
            )
        except Exception as exc:  # source-level isolation is part of the contract
            errors.append({"competition_id": competition_id, "error": str(exc)})
    fixtures.sort(key=lambda item: item["kickoff_at"])
    return {
        "provider": "ESPN",
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "horizon_days": horizon_days,
        "lookback_days": lookback_days,
        "fixtures": fixtures,
        "errors": errors,
        "access_allowed": True,
        "rights_status": "operator_authorization_reference_supplied",
        "authorization_reference": authorization_reference,
    }
