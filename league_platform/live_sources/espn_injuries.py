"""Bounded ESPN league injury-report observations.

ESPN exposes a public league injury endpoint, but its soccer coverage can be
empty even when real-world injuries exist.  This adapter therefore treats a
valid empty array as ``observed_empty`` rather than as proof that every player
is healthy.  Rows are provider evidence only: the endpoint has no
fixture-specific publication clock and its commercial reuse terms have not
been verified, so nothing from this module is model eligible.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import urlparse

from league_platform.live_sources.espn import (
    ESPN_ALLOWED_HOSTS,
    ESPN_CODES,
    _safe_opener,
)
from league_platform.source_rights import SourceId, rights_blocked_envelope


ESPN_PRIMARY_HOST = "site.api.espn.com"
ESPN_WEB_HOST = "site.web.api.espn.com"
ESPN_INJURY_PATH_PREFIX = "/apis/site/v2/sports/soccer/"
ESPN_TERMS_URL = "https://disneytermsofuse.com/english/"
ESPN_RIGHTS_STATUS = "blocked_pending_express_written_permission"
MAX_INJURY_BYTES = 4 * 1024 * 1024
MAX_TEAM_REPORTS = 80
MAX_INJURIES_PER_TEAM = 80
DEFAULT_INJURY_HORIZON_HOURS = 168
_NUMERIC_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_NON_INJURY_STATUSES = frozenset({"active", "normal service"})


def _text(value: object, *, max_length: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= max_length else None


def _numeric_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    candidate = str(value).strip() if isinstance(value, (str, int)) else ""
    return candidate if _NUMERIC_ID_RE.fullmatch(candidate) else None


def _position(value: object) -> str | None:
    if isinstance(value, Mapping):
        return _text(
            value.get("abbreviation") or value.get("displayName") or value.get("name"),
            max_length=80,
        )
    return _text(value, max_length=80)


def _status(value: object) -> str | None:
    if isinstance(value, Mapping):
        return _text(
            value.get("displayName") or value.get("name") or value.get("abbreviation"),
            max_length=80,
        )
    return _text(value, max_length=80)


def _validate_injury_url(url: str, *, competition_id: str) -> None:
    code = ESPN_CODES.get(competition_id)
    parsed = urlparse(url)
    expected_path = f"{ESPN_INJURY_PATH_PREFIX}{code}/injuries" if code else None
    if (
        expected_path is None
        or parsed.scheme != "https"
        or parsed.hostname not in ESPN_ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ESPN injury URL is not allowlisted")


def _injury_url(competition_id: str, host: str) -> str:
    if host not in ESPN_ALLOWED_HOSTS or competition_id not in ESPN_CODES:
        raise ValueError("unknown ESPN injury competition or host")
    url = f"https://{host}{ESPN_INJURY_PATH_PREFIX}{ESPN_CODES[competition_id]}/injuries"
    _validate_injury_url(url, competition_id=competition_id)
    return url


def _read_injury_response(
    opener: Callable[..., object] | object,
    request: urllib.request.Request,
    *,
    competition_id: str,
) -> bytes:
    """Read one exact endpoint response with a lane-specific size cap."""

    open_method = getattr(opener, "open", None) or opener
    response = open_method(request, timeout=30)  # type: ignore[operator]
    with response:
        final_url = str(getattr(response, "geturl", lambda: request.full_url)())
        # The shared ESPN reader permits all ESPN API paths. This lane is more
        # sensitive: a response from any other path would be mislabelled as an
        # injury report, so validate the final URL against the exact endpoint.
        _validate_injury_url(final_url, competition_id=competition_id)
        payload = response.read(MAX_INJURY_BYTES + 1)
    if not isinstance(payload, bytes):
        raise TypeError("ESPN injury response must be bytes")
    if len(payload) > MAX_INJURY_BYTES:
        raise ValueError("ESPN injury response exceeded size limit")
    return payload


def _fixture_links(
    fixture_index: Mapping[str, list[tuple[str, str]]],
    provider_team_id: str,
) -> tuple[list[str], list[str]]:
    rows = fixture_index.get(provider_team_id)
    if not isinstance(rows, list):
        return [], []
    fixture_ids: set[str] = set()
    kickoffs: set[str] = set()
    for row in rows[:64]:
        if not isinstance(row, tuple) or len(row) != 2:
            continue
        fixture_id = _text(row[0], max_length=160)
        kickoff = _text(row[1], max_length=80)
        if fixture_id:
            fixture_ids.add(fixture_id)
        if kickoff:
            kickoffs.add(kickoff)
    return sorted(fixture_ids), sorted(kickoffs)


def _team_identity(bucket: Mapping[str, object]) -> tuple[str, str, str | None] | None:
    team = bucket.get("team") if isinstance(bucket.get("team"), Mapping) else {}
    provider_team_id = _numeric_id(bucket.get("id")) or _numeric_id(team.get("id"))
    name = _text(
        bucket.get("displayName")
        or bucket.get("name")
        or team.get("displayName")
        or team.get("name"),
        max_length=160,
    )
    if not provider_team_id or not name:
        return None
    abbreviation = _text(
        bucket.get("abbreviation") or team.get("abbreviation"),
        max_length=16,
    )
    return provider_team_id, name, abbreviation


def _injury(row: object) -> dict | None:
    if not isinstance(row, Mapping):
        return None
    athlete = row.get("athlete") if isinstance(row.get("athlete"), Mapping) else {}
    provider_player_id = _numeric_id(athlete.get("id") or row.get("id"))
    name = _text(
        athlete.get("displayName")
        or athlete.get("fullName")
        or row.get("displayName")
        or row.get("name"),
        max_length=160,
    )
    status = _status(row.get("status"))
    if not provider_player_id or not name or not status:
        return None
    if status.casefold() in _NON_INJURY_STATUSES:
        return None
    details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
    injury_type = row.get("type") if isinstance(row.get("type"), Mapping) else {}
    reason = _text(
        details.get("detail")
        or injury_type.get("displayName")
        or injury_type.get("name")
        or row.get("reason"),
        max_length=160,
    )
    suspension_text = " ".join(value for value in (status, reason or "") if value).casefold()
    status_type = "suspension_report" if "suspend" in suspension_text else "injury_report"
    result = {
        "provider_player_id": provider_player_id,
        "name": name,
        "position": _position(athlete.get("position") or row.get("position")),
        "status": status,
        "status_type": status_type,
        "reason": reason,
        # ESPN does not document this as the row publication time.  Preserve
        # it as provider metadata and use only the actual retrieval clock for
        # causal cutoffs.
        "provider_date": _text(row.get("date"), max_length=80),
        "return_date": _text(details.get("returnDate"), max_length=80),
        "note": _text(row.get("shortComment"), max_length=240),
        "expected_minutes": None,
        "replacement_value": None,
    }
    return result


def parse_espn_injuries_payload(
    payload: bytes,
    *,
    competition_id: str,
    retrieved_at: datetime,
    url: str,
    fixture_index: Mapping[str, list[tuple[str, str]]],
    authorization_reference: str | None = None,
) -> dict:
    """Parse one league response without treating omitted teams as healthy."""

    _validate_injury_url(url, competition_id=competition_id)
    if retrieved_at.tzinfo is None:
        raise ValueError("ESPN injury retrieved_at must be timezone-aware")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("ESPN injury response is not valid JSON") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("injuries"), list):
        raise ValueError("ESPN injury response must contain an injuries array")
    observed = retrieved_at.astimezone(timezone.utc)
    source = {
        "name": "ESPN injury report",
        "url": url,
        "retrieved_at": observed.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "provider_timestamp": _text(data.get("timestamp"), max_length=80),
        "time_basis": "observed_at_no_row_published_at",
        "policy": {
            "allow_model": False,
            "rights_status": (
                "operator_authorization_reference_supplied"
                if authorization_reference
                else ESPN_RIGHTS_STATUS
            ),
            "authorization_reference": authorization_reference,
            "commercial_reuse_verified_by_code": False,
            "terms_url": ESPN_TERMS_URL,
        },
    }
    reports: list[dict] = []
    for bucket in data["injuries"][:MAX_TEAM_REPORTS]:
        if not isinstance(bucket, Mapping):
            continue
        identity = _team_identity(bucket)
        if identity is None:
            continue
        provider_team_id, team_name, abbreviation = identity
        raw_rows = bucket.get("injuries")
        if not isinstance(raw_rows, list):
            continue
        injuries: list[dict] = []
        seen: set[str] = set()
        for raw_row in raw_rows[:MAX_INJURIES_PER_TEAM]:
            parsed = _injury(raw_row)
            if parsed is None:
                continue
            fingerprint = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            injuries.append(parsed)
        scheduled_fixture_ids, scheduled_kickoffs = _fixture_links(fixture_index, provider_team_id)
        reports.append(
            {
                "competition_id": competition_id,
                "provider_team_id": provider_team_id,
                "team_name": team_name,
                "team_abbreviation": abbreviation,
                "report_status": "published",
                "injuries": injuries,
                "reported_player_count": len(injuries),
                "scheduled_fixture_ids": scheduled_fixture_ids,
                "scheduled_kickoffs": scheduled_kickoffs,
                "retrieved_at": observed.isoformat(),
                "model_eligible": False,
                "enters_model": False,
                "model_exclusion_reason": (
                    "league_report_not_fixture_complete_and_provider_terms_require_review"
                ),
                "source": source,
            }
        )
    reports.sort(key=lambda item: (item["provider_team_id"], item["team_name"]))
    player_count = sum(int(report["reported_player_count"]) for report in reports)
    return {
        "competition_id": competition_id,
        "provider": "ESPN injury report",
        "retrieved_at": observed.isoformat(),
        "status": "published_reports" if reports else "observed_empty",
        "provider_status": _text(data.get("status"), max_length=80),
        "season": data.get("season") if isinstance(data.get("season"), Mapping) else {},
        "reports": reports,
        "published_team_count": len(reports),
        "reported_player_count": player_count,
        "healthy_team_count": None,
        "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
        "model_eligible": False,
        "enters_model": False,
        "model_exclusion_reason": (
            "league_report_not_fixture_complete_and_provider_terms_require_review"
        ),
        "source": source,
    }


def _candidate_competitions(
    fixtures: list[dict],
    *,
    reference_time: datetime,
    horizon_hours: int,
) -> dict[str, dict[str, list[tuple[str, str]]]]:
    cutoff = reference_time.astimezone(timezone.utc) + timedelta(hours=max(0, int(horizon_hours)))
    candidates: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for fixture in fixtures:
        if not isinstance(fixture, Mapping) or fixture.get("status") != "upcoming":
            continue
        competition_id = _text(fixture.get("competition_id"), max_length=80)
        if competition_id not in ESPN_CODES:
            continue
        try:
            kickoff = datetime.fromisoformat(str(fixture.get("kickoff_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff.tzinfo is None:
            continue
        kickoff = kickoff.astimezone(timezone.utc)
        if not reference_time.astimezone(timezone.utc) <= kickoff <= cutoff:
            continue
        fixture_id = _text(fixture.get("id"), max_length=160)
        if not fixture_id:
            continue
        by_team = candidates.setdefault(competition_id, {})
        for side in ("home", "away"):
            provider_team_id = _numeric_id(fixture.get(f"{side}_provider_team_id"))
            if provider_team_id:
                by_team.setdefault(provider_team_id, []).append((fixture_id, kickoff.isoformat()))
    return candidates


def fetch_espn_injuries(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_hours: int = DEFAULT_INJURY_HORIZON_HOURS,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: str | None = None,
) -> dict:
    """Fetch reports only after an operator supplies a written-rights reference.

    A public endpoint is not itself permission for automated access. ESPN links
    its products to Disney's terms, whose current general restrictions prohibit
    automated extraction and commercial/business use without express written
    permission. The default therefore returns a structured policy block before
    constructing an opener or performing any network request.
    """

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    return {
        **rights_blocked_envelope(
            SourceId.ESPN_INJURY_REPORTS,
            provider="ESPN injury report",
            checked_at=reference_time.astimezone(timezone.utc).isoformat(),
            empty_fields=("competition_reports", "reports", "fallbacks"),
            authorization_reference=authorization_reference,
        ),
        "authorization_required": "express_written_permission",
        "terms_url": ESPN_TERMS_URL,
        "enters_model": False,
        "model_exclusion_reason": "provider_rights_blocked",
        "horizon_hours": int(horizon_hours),
        "requested_competitions": 0,
        "observed_competitions": 0,
        "observed_empty_competitions": 0,
        "published_team_count": 0,
        "reported_player_count": 0,
        "healthy_team_count": None,
        "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
    }
    # Retained below as a parser/reference implementation only.  Policy v260
    # deliberately returns above before constructing an opener or request.
    authorization_reference = _text(authorization_reference, max_length=240)
    if authorization_reference is None:
        return {
            "provider": "ESPN injury report",
            "retrieved_at": None,
            "checked_at": reference_time.astimezone(timezone.utc).isoformat(),
            "status": "rights_blocked",
            "horizon_hours": int(horizon_hours),
            "requested_competitions": 0,
            "observed_competitions": 0,
            "observed_empty_competitions": 0,
            "competition_reports": [],
            "reports": [],
            "published_team_count": 0,
            "reported_player_count": 0,
            "healthy_team_count": None,
            "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
            "fallbacks": [],
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
    candidates = _candidate_competitions(
        fixtures,
        reference_time=reference_time,
        horizon_hours=horizon_hours,
    )
    fetcher = opener or _safe_opener()

    def fetch_one(
        competition_id: str,
        fixture_index: Mapping[str, list[tuple[str, str]]],
    ) -> tuple[dict, list[str]]:
        failures: list[str] = []
        for host in (ESPN_PRIMARY_HOST, ESPN_WEB_HOST):
            url = _injury_url(competition_id, host)
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
            )
            try:
                payload = _read_injury_response(
                    fetcher,
                    request,
                    competition_id=competition_id,
                )
                observed_at = reference_time if now is not None else datetime.now(timezone.utc)
                return (
                    parse_espn_injuries_payload(
                        payload,
                        competition_id=competition_id,
                        retrieved_at=observed_at,
                        url=url,
                        fixture_index=fixture_index,
                        authorization_reference=authorization_reference,
                    ),
                    failures,
                )
            except Exception as exc:  # noqa: BLE001 - fixed source isolation
                failures.append(f"{url}: {exc}")
        raise OSError("; ".join(failures))

    competition_reports: list[dict] = []
    errors: list[dict] = []
    fallbacks: list[dict] = []
    if candidates:
        with ThreadPoolExecutor(
            max_workers=min(4, len(candidates)),
            thread_name_prefix="matchline-espn-injuries",
        ) as executor:
            futures = {
                executor.submit(fetch_one, competition_id, fixture_index): competition_id
                for competition_id, fixture_index in candidates.items()
            }
            for future in as_completed(futures):
                competition_id = futures[future]
                try:
                    report, failures = future.result()
                except Exception as exc:  # noqa: BLE001 - one league must not poison peers
                    errors.append(
                        {
                            "competition_id": competition_id,
                            "url": _injury_url(competition_id, ESPN_PRIMARY_HOST),
                            "error": str(exc),
                        }
                    )
                    continue
                competition_reports.append(report)
                if failures:
                    fallbacks.append(
                        {
                            "competition_id": competition_id,
                            "primary_host": ESPN_PRIMARY_HOST,
                            "fallback_host": ESPN_WEB_HOST,
                            "errors": failures,
                        }
                    )
    competition_reports.sort(key=lambda item: item["competition_id"])
    reports = [report for competition in competition_reports for report in competition["reports"]]
    player_count = sum(int(report["reported_player_count"]) for report in reports)
    if (
        competition_reports
        and not errors
        and all(item.get("status") == "observed_empty" for item in competition_reports)
    ):
        status = "observed_empty"
    elif competition_reports and not errors:
        status = "fresh"
    elif competition_reports:
        status = "degraded"
    elif errors:
        status = "unavailable"
    else:
        status = "not_requested"
    observed_times = [
        datetime.fromisoformat(item["retrieved_at"])
        for item in competition_reports
        if isinstance(item.get("retrieved_at"), str)
    ]
    return {
        "provider": "ESPN injury report",
        "retrieved_at": max(observed_times, default=reference_time).isoformat(),
        "status": status,
        "horizon_hours": int(horizon_hours),
        "requested_competitions": len(candidates),
        "observed_competitions": len(competition_reports),
        "observed_empty_competitions": sum(
            item.get("status") == "observed_empty" for item in competition_reports
        ),
        "competition_reports": competition_reports,
        "reports": reports,
        "published_team_count": len(reports),
        "reported_player_count": player_count,
        "healthy_team_count": None,
        "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
        "fallbacks": sorted(fallbacks, key=lambda item: item["competition_id"]),
        "errors": sorted(errors, key=lambda item: item["competition_id"]),
        "model_eligible": False,
        "enters_model": False,
        "model_exclusion_reason": (
            "league_report_not_fixture_complete_and_provider_terms_require_review"
        ),
        "access_allowed": True,
        "rights_status": "operator_authorization_reference_supplied",
        "authorization_reference": authorization_reference,
        "terms_url": ESPN_TERMS_URL,
    }


__all__ = [
    "DEFAULT_INJURY_HORIZON_HOURS",
    "ESPN_INJURY_PATH_PREFIX",
    "ESPN_RIGHTS_STATUS",
    "ESPN_TERMS_URL",
    "MAX_INJURY_BYTES",
    "fetch_espn_injuries",
    "parse_espn_injuries_payload",
]
