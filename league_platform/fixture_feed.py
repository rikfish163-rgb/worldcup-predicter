"""Provider-neutral access to current and archived fixture sections.

New snapshots store the canonical schedule/result feed under ``fixture_feed``.
Archived snapshots written before that contract existed retain the original
``espn`` section and remain replayable.  A present but malformed current
section never falls back to the legacy provider, because doing so would hide a
corrupt or tampered hand-off.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from league_platform.identity import canonical_team_name, team_id


CURRENT_FIXTURE_FEED_KEY = "fixture_feed"
LEGACY_FIXTURE_FEED_KEY = "espn"
MULTI_SOURCE_FIXTURE_PROVIDER = "OpenFootball + CFL official"
PREDICTION_COVERAGE_SCHEMA_VERSION = "matchline.prediction_coverage.v1"


def _fixture_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """Sort by an absolute kickoff when one is available.

    Feed rows may carry equivalent timestamps with different offsets.  Sorting
    their raw strings can put a later UTC fixture before an earlier one and
    makes the result depend on which provider emitted the row.  Date-only rows
    retain their calendar date as the deterministic fallback.
    """

    kickoff = _aware_time(row.get("kickoff_at"))
    sort_time = kickoff.isoformat() if kickoff is not None else str(row.get("kickoff_date") or "")
    return (sort_time, str(row.get("id") or ""))


def _dedupe_rows_by_id(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
    reject_conflicts: bool = False,
) -> list[dict[str, Any]]:
    """Keep one deterministic row per canonical ID.

    Window projections are often assembled from more than one source list and
    can legitimately repeat the same immutable fixture.  Exact duplicate
    payloads are collapsed.  When a canonical list contains two different
    payloads for one ID, silently selecting one would hide an identity
    conflict, so callers can request a fail-closed ``ValueError``.
    """

    result: list[dict[str, Any]] = []
    seen: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        fixture_id = row.get("id")
        if isinstance(fixture_id, str) and fixture_id.strip():
            previous = seen.get(fixture_id)
            if previous is not None:
                if reject_conflicts and row != previous:
                    raise ValueError(f"conflicting duplicate {label} fixture ID: {fixture_id}")
                continue
            seen[fixture_id] = row
        result.append(row)
    return result


def merge_current_fixture_feeds(
    feeds: list[Mapping[str, Any]],
    *,
    retrieved_at: str,
) -> dict[str, Any]:
    """Union independent current feeds without relabelling their rows.

    The top-level provider is an aggregation label only. Every fixture keeps
    the original provider metadata and lineage supplied by its fact source.
    Duplicate canonical IDs fail closed instead of silently choosing a row.
    """

    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        raise ValueError("merged fixture feed retrieved_at is required")
    if not feeds:
        raise ValueError("at least one current fixture feed is required")

    list_keys = (
        "fixtures",
        "date_only_fixtures",
        "recent_results",
        "upcoming_3_days",
        "upcoming_7_days",
        "errors",
    )
    combined: dict[str, list[dict[str, Any]]] = {key: [] for key in list_keys}
    providers: list[str] = []
    contracts: list[dict[str, Any]] = []
    provider_status: list[dict[str, Any]] = []
    seen_fixture_ids: set[str] = set()
    for feed in feeds:
        provider = feed.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("current fixture feed provider is missing")
        providers.append(provider)
        for key in list_keys:
            rows = feed.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"current fixture feed {provider} {key} is invalid")
            if key == "errors":
                combined[key].extend(
                    {"provider": provider, **row}
                    if "provider" not in row
                    else dict(row)
                    for row in rows
                )
            else:
                combined[key].extend(dict(row) for row in rows)
        for row in feed.get("fixtures", []):
            fixture_id = row.get("id")
            if not isinstance(fixture_id, str) or not fixture_id:
                raise ValueError(f"current fixture feed {provider} row is missing an ID")
            if fixture_id in seen_fixture_ids:
                raise ValueError(f"duplicate canonical fixture ID: {fixture_id}")
            seen_fixture_ids.add(fixture_id)
        contract = feed.get("source_contract")
        if isinstance(contract, Mapping):
            contracts.append(dict(contract))
        provider_status.append(
            {
                "provider": provider,
                "status": feed.get("status"),
                "retrieved_at": feed.get("retrieved_at"),
                "fixture_count": len(feed.get("fixtures", [])),
                "error_count": len(feed.get("errors", [])),
            }
        )

    # Canonical exact rows are authoritative over their date-only shadow.  A
    # date-only row with the same ID therefore must not create a second
    # coverage candidate.  Conflicting date-only payloads are quarantined;
    # identical repeats are harmless and collapse to one row.
    combined["date_only_fixtures"] = _dedupe_rows_by_id(
        combined["date_only_fixtures"],
        label="date-only",
        reject_conflicts=True,
    )
    exact_ids = {
        str(row.get("id"))
        for row in combined["fixtures"]
        if isinstance(row.get("id"), str) and row.get("id").strip()
    }
    combined["date_only_fixtures"] = [
        row
        for row in combined["date_only_fixtures"]
        if row.get("id") not in exact_ids
    ]
    combined["fixtures"].sort(key=_fixture_sort_key)
    combined["date_only_fixtures"].sort(key=_fixture_sort_key)
    kickoff_by_id = {
        row.get("id"): _fixture_sort_key(row)
        for row in combined["fixtures"]
        if isinstance(row.get("id"), str)
    }
    for key in ("recent_results", "upcoming_3_days", "upcoming_7_days"):
        combined[key] = _dedupe_rows_by_id(
            combined[key],
            label=key,
            reject_conflicts=False,
        )
        combined[key].sort(
            key=lambda row: kickoff_by_id.get(
                row.get("id"), _fixture_sort_key(row)
            )
        )
    has_rows = bool(combined["fixtures"] or combined["date_only_fixtures"])
    all_ok = all(row.get("status") == "ok" for row in provider_status)
    status = (
        "ok"
        if has_rows and all_ok and not combined["errors"]
        else "degraded"
        if has_rows
        else "unavailable"
    )
    return {
        "provider": MULTI_SOURCE_FIXTURE_PROVIDER,
        "retrieved_at": retrieved_at,
        "status": status,
        **combined,
        "provider_status": provider_status,
        "source_contract": {
            "aggregation_role": "provider-neutral union; row source remains authoritative",
            "fact_sources": providers,
            "source_contracts": contracts,
            "identity_policy": "provider-scoped immutable fixture IDs; duplicates fail closed",
        },
        "diagnostics": {
            "provider_count": len(providers),
            "fixture_count": len(combined["fixtures"]),
            "date_only_fixture_count": len(combined["date_only_fixtures"]),
            "error_count": len(combined["errors"]),
        },
    }


def _aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validated_official_source(
    fixture: Mapping[str, Any],
    *,
    provider: str,
    reference_time: datetime,
) -> tuple[dict[str, Any], str, datetime] | None:
    source = fixture.get("source")
    if not isinstance(source, Mapping) or source.get("name") != provider:
        return None
    raw_hash = source.get("raw_sha256")
    url = urlparse(str(source.get("url") or ""))
    observed = _aware_time(source.get("retrieved_at"))
    native_fixture_id = source.get("native_fixture_id")
    if (
        not isinstance(raw_hash, str)
        or len(raw_hash) != 64
        or any(character not in string.hexdigits for character in raw_hash)
        or url.scheme != "https"
        or not url.hostname
        or observed is None
        or observed > reference_time.astimezone(timezone.utc) + timedelta(minutes=5)
        or not isinstance(native_fixture_id, (str, int))
        or not str(native_fixture_id).strip()
    ):
        return None
    return dict(source), str(native_fixture_id), observed


def _structured_venue(
    value: object,
    *,
    country: str,
    country_code: str,
    source: Mapping[str, Any],
) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        name = str(value.get("name") or "").strip()
        city = str(value.get("city") or "").strip()
    else:
        text = str(value or "").strip()
        if not text:
            return None
        name, separator, city = text.rpartition(",")
        if not separator:
            name, city = text, ""
        name, city = name.strip(), city.strip()
    if not name:
        return None
    venue: dict[str, Any] = {
        "name": name,
        "country": country,
        "country_code": country_code,
        "source": deepcopy(dict(source)),
    }
    if city:
        venue["city"] = city
    return venue


def apply_official_schedule_overlay(
    feed: Mapping[str, Any],
    official_fixtures: list[Mapping[str, Any]],
    *,
    competition_id: str,
    provider: str,
    provider_id_key: str,
    country: str,
    country_code: str,
    reference_time: datetime,
    max_kickoff_delta: timedelta = timedelta(days=7),
) -> dict[str, Any]:
    """Attach first-party schedule fields to stable canonical fixture rows.

    The fixture ID, row-level source and lineage remain owned by the canonical
    feed.  Only an unambiguous competition-scoped home/away identity pair can
    contribute an official kickoff or venue, and every contributed field
    retains its own source and observation clock.
    """

    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("official schedule overlay reference_time must be timezone-aware")
    if max_kickoff_delta <= timedelta(0):
        raise ValueError("official schedule overlay max_kickoff_delta must be positive")
    result = deepcopy(dict(feed))
    fixtures = result.get("fixtures")
    if not isinstance(fixtures, list) or any(not isinstance(row, dict) for row in fixtures):
        raise ValueError("official schedule overlay fixture feed is invalid")
    if any(not isinstance(row, Mapping) for row in official_fixtures):
        raise ValueError("official schedule overlay provider rows are invalid")

    canonical_by_pair: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(fixtures):
        if row.get("competition_id") != competition_id or row.get("status") != "upcoming":
            continue
        home = row.get("home_team")
        away = row.get("away_team")
        if not isinstance(home, str) or not isinstance(away, str) or not home or not away:
            continue
        canonical_by_pair.setdefault(
            (team_id(competition_id, home), team_id(competition_id, away)), []
        ).append(index)

    official_by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in official_fixtures:
        if row.get("competition_id") != competition_id or row.get("status") != "upcoming":
            continue
        home = row.get("home_team")
        away = row.get("away_team")
        if not isinstance(home, str) or not isinstance(away, str) or not home or not away:
            continue
        official_by_pair.setdefault(
            (team_id(competition_id, home), team_id(competition_id, away)), []
        ).append(row)

    samples: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "schema_version": "matchline.official-schedule-overlay.v1",
        "competition_id": competition_id,
        "provider": provider,
        "join_basis": "exact_competition_and_explicit_canonical_team_pair",
        "reference_at": reference_time.astimezone(timezone.utc).isoformat(),
        "canonical_candidate_count": sum(len(rows) for rows in canonical_by_pair.values()),
        "official_candidate_count": sum(len(rows) for rows in official_by_pair.values()),
        "matched_count": 0,
        "kickoff_changed_count": 0,
        "venue_attached_count": 0,
        "ambiguous_count": 0,
        "invalid_source_count": 0,
        "time_mismatch_count": 0,
        "samples": samples,
    }

    for pair, canonical_indexes in canonical_by_pair.items():
        official_rows = official_by_pair.get(pair, [])
        if not official_rows:
            continue
        if len(canonical_indexes) != 1 or len(official_rows) != 1:
            diagnostics["ambiguous_count"] += 1
            if len(samples) < 12:
                samples.append(
                    {
                        "reason": "ambiguous_team_pair",
                        "canonical_fixture_ids": [
                            fixtures[index].get("id") for index in canonical_indexes
                        ],
                        "official_fixture_ids": [row.get("id") for row in official_rows],
                    }
                )
            continue
        index = canonical_indexes[0]
        canonical = fixtures[index]
        official = official_rows[0]
        source_contract = _validated_official_source(
            official,
            provider=provider,
            reference_time=reference_time,
        )
        canonical_kickoff = _aware_time(canonical.get("kickoff_at"))
        official_kickoff = _aware_time(official.get("kickoff_at"))
        if source_contract is None or canonical_kickoff is None or official_kickoff is None:
            diagnostics["invalid_source_count"] += 1
            if len(samples) < 12:
                samples.append(
                    {
                        "reason": "invalid_source_or_kickoff",
                        "canonical_fixture_id": canonical.get("id"),
                        "official_fixture_id": official.get("id"),
                    }
                )
            continue
        delta = official_kickoff - canonical_kickoff
        if abs(delta) > max_kickoff_delta:
            diagnostics["time_mismatch_count"] += 1
            if len(samples) < 12:
                samples.append(
                    {
                        "reason": "kickoff_delta_exceeds_bound",
                        "canonical_fixture_id": canonical.get("id"),
                        "official_fixture_id": official.get("id"),
                        "kickoff_delta_seconds": int(delta.total_seconds()),
                    }
                )
            continue

        official_source, native_fixture_id, observed_at = source_contract
        venue = _structured_venue(
            official.get("venue"),
            country=country,
            country_code=country_code,
            source=official_source,
        )
        fields = ["kickoff_at"]
        field_sources = (
            deepcopy(canonical.get("field_sources"))
            if isinstance(canonical.get("field_sources"), dict)
            else {}
        )
        field_sources["kickoff_at"] = deepcopy(official_source)
        provider_fixture_ids = (
            deepcopy(canonical.get("provider_fixture_ids"))
            if isinstance(canonical.get("provider_fixture_ids"), dict)
            else {}
        )
        provider_fixture_ids[provider_id_key] = native_fixture_id
        canonical.update(
            {
                "kickoff_at": official_kickoff.isoformat(),
                "kickoff_date": official_kickoff.date().isoformat(),
                "kickoff_time_quality": "exact",
                "kickoff_time_source": provider,
                "kickoff_time_observed_at": observed_at.isoformat(),
                "kickoff_timezone": "UTC",
                "provider_fixture_ids": provider_fixture_ids,
            }
        )
        if venue is not None:
            canonical["venue"] = venue
            field_sources["venue"] = deepcopy(official_source)
            fields.append("venue")
            diagnostics["venue_attached_count"] += 1
        canonical["field_sources"] = field_sources
        canonical["schedule_overlay"] = {
            "provider": provider,
            "provider_fixture_id": native_fixture_id,
            "join_basis": "exact_competition_and_explicit_canonical_team_pair",
            "observed_at": observed_at.isoformat(),
            "prior_kickoff_at": canonical_kickoff.isoformat(),
            "kickoff_delta_seconds": int(delta.total_seconds()),
            "fields": fields,
        }
        diagnostics["matched_count"] += 1
        if delta:
            diagnostics["kickoff_changed_count"] += 1

    fixtures.sort(key=_fixture_sort_key)
    by_id = {
        row.get("id"): row for row in fixtures if isinstance(row.get("id"), str)
    }
    recent = result.get("recent_results", [])
    if isinstance(recent, list):
        result["recent_results"] = sorted(
            [deepcopy(by_id.get(row.get("id"), row)) for row in recent if isinstance(row, dict)],
            key=_fixture_sort_key,
        )
    reference = reference_time.astimezone(timezone.utc)
    for key, days in (("upcoming_3_days", 3), ("upcoming_7_days", 7)):
        horizon = reference + timedelta(days=days)
        result[key] = [
            deepcopy(row)
            for row in fixtures
            if row.get("status") == "upcoming"
            and (kickoff := _aware_time(row.get("kickoff_at"))) is not None
            and reference < kickoff <= horizon
        ]
    diagnostics_by_competition = (
        deepcopy(result.get("schedule_overlay_diagnostics_by_competition"))
        if isinstance(result.get("schedule_overlay_diagnostics_by_competition"), dict)
        else {}
    )
    diagnostics_by_competition[competition_id] = diagnostics
    result["schedule_overlay_diagnostics_by_competition"] = diagnostics_by_competition
    if competition_id == "premier-league" or not isinstance(
        result.get("schedule_overlay_diagnostics"), dict
    ):
        # Keep the original Premier League diagnostic contract stable while
        # the per-competition map records every additional field authority.
        result["schedule_overlay_diagnostics"] = diagnostics
    contract = (
        deepcopy(result.get("source_contract"))
        if isinstance(result.get("source_contract"), dict)
        else {}
    )
    field_sources = contract.get("field_sources")
    if not isinstance(field_sources, list):
        field_sources = []
    if provider not in field_sources:
        field_sources.append(provider)
    contract.update(
        {
            "field_sources": field_sources,
            "field_source_policy": (
                "canonical fixture ID, row source and lineage are retained; "
                "official fields require an exact competition/team-pair join"
            ),
        }
    )
    result["source_contract"] = contract
    return result


def fixture_feed_key(snapshot: Mapping[str, Any]) -> str | None:
    """Return the selected snapshot key without inspecting provider labels."""

    if CURRENT_FIXTURE_FEED_KEY in snapshot:
        return CURRENT_FIXTURE_FEED_KEY
    if LEGACY_FIXTURE_FEED_KEY in snapshot:
        return LEGACY_FIXTURE_FEED_KEY
    return None


def fixture_feed_section(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the current feed or the immutable legacy section.

    Provider identity remains inside the returned section and is validated by
    the caller.  This helper only chooses the schema location; it never
    relabels one provider as another.
    """

    key = fixture_feed_key(snapshot)
    if key is None:
        return {}
    section = snapshot.get(key)
    return section if isinstance(section, Mapping) else {}


def fixture_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return dictionary fixture rows, failing closed on a malformed list."""

    rows = fixture_feed_section(snapshot).get("fixtures")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return []
    return rows


def snapshot_fixture_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the best available immutable fixture catalog for diagnostics.

    ``matches`` is the compact snapshot projection consumed by the existing
    read model.  New snapshots also carry the provider-neutral ``fixture_feed``
    section; using both its exact and date-only rows keeps coverage diagnostics
    from silently reporting zero fixtures when a future kickoff is unresolved.
    Rows from all sections are de-duplicated by their canonical fixture ID,
    with the compact row taking precedence when both are present.
    """

    raw_matches = snapshot.get("matches")
    matches = [dict(row) for row in raw_matches if isinstance(row, Mapping)] if isinstance(raw_matches, list) else []
    feed = fixture_rows(snapshot)
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    feed_section = fixture_feed_section(snapshot)
    date_only = feed_section.get("date_only_fixtures")
    date_only_rows = (
        [dict(row) for row in date_only if isinstance(row, Mapping)]
        if isinstance(date_only, list)
        else []
    )
    for row in [*matches, *feed, *date_only_rows]:
        fixture_id = row.get("id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            # A row without a stable ID cannot be audited or joined, so it is
            # intentionally excluded rather than assigned a synthetic ID.
            continue
        if fixture_id in seen:
            continue
        seen.add(fixture_id)
        combined.append(row)
    combined.sort(key=_fixture_sort_key)
    return combined


def upcoming_fixture_rows(
    snapshot: Mapping[str, Any],
    *,
    as_of: datetime | None = None,
    horizon: timedelta | None = None,
    include_unparseable_kickoff: bool = False,
) -> list[dict[str, Any]]:
    """Select upcoming fixtures without manufacturing a kickoff timestamp.

    A parseable kickoff is filtered against ``as_of`` and ``horizon``.  A row
    with an invalid/missing kickoff can optionally be retained so callers can
    emit an explicit ``unavailable`` record instead of dropping its fixture
    identity.  ``as_of`` is expected to be timezone-aware; a naive value is
    treated as unavailable and therefore does not impose a time filter.
    """

    if horizon is not None:
        if not isinstance(horizon, timedelta):
            raise ValueError("horizon must be a timedelta or None")
        if horizon < timedelta(0):
            raise ValueError("horizon must be non-negative")
    reference = _aware_time(as_of) if as_of is not None else None
    horizon_end = reference + horizon if reference is not None and horizon is not None else None
    selected: list[dict[str, Any]] = []
    for row in snapshot_fixture_rows(snapshot):
        if row.get("status") != "upcoming":
            continue
        kickoff = _aware_time(row.get("kickoff_at"))
        if kickoff is None:
            if not include_unparseable_kickoff:
                continue
            # Date-only schedule rows are common in a full-season feed.  Keep
            # them eligible for an explicit unavailable record only when the
            # published calendar date intersects the requested window; do not
            # let a whole season of unresolved kickoff times inflate a bounded
            # three/seven-day coverage denominator.
            date_value = row.get("kickoff_date")
            try:
                kickoff_date = datetime.fromisoformat(str(date_value)).date()
            except (TypeError, ValueError):
                kickoff_date = None
            if kickoff_date is not None:
                if reference is not None and kickoff_date < reference.date():
                    continue
                if horizon_end is not None and kickoff_date > horizon_end.date():
                    continue
            selected.append(row)
            continue
        if reference is not None and kickoff <= reference:
            continue
        if horizon_end is not None and kickoff > horizon_end:
            continue
        selected.append(row)
    selected.sort(key=_fixture_sort_key)
    return selected


def fixture_team_identities(row: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Build deterministic team identities when a fixture exposes team facts."""

    competition = row.get("competition_id")
    result: dict[str, dict[str, Any]] = {}
    for side in ("home", "away"):
        input_name = row.get(f"{side}_team")
        explicit_id = row.get(f"{side}_team_id") or row.get(f"{side}_provider_team_id")
        canonical = input_name
        if isinstance(competition, str) and competition.strip() and isinstance(input_name, str):
            canonical = canonical_team_name(competition, input_name)
        if input_name is None and explicit_id is None:
            continue
        if not isinstance(input_name, str) and explicit_id is None:
            continue
        team_value = explicit_id
        if team_value is None and isinstance(competition, str) and isinstance(input_name, str):
            team_value = team_id(competition, input_name)
        result[side] = {
            "input_name": input_name,
            "canonical_name": canonical,
            "team_id": team_value,
            "alias_applied": bool(
                isinstance(input_name, str)
                and isinstance(canonical, str)
                and input_name != canonical
            ),
        }
    return result or None


def blocked_fixture_record(
    row: Mapping[str, Any],
    *,
    reason: str,
    message: str | None = None,
    status: str = "blocked",
) -> dict[str, Any]:
    """Create a stable per-fixture block record without probability fields."""

    fixture_id = row.get("id") or row.get("fixture_id")
    identities = fixture_team_identities(row)
    # Preserve the historical compact diagnostic shape for an ID-only row.
    # Once team/competition facts are available, emit the full stable schema
    # so the caller can audit exactly which fixture was blocked.
    result: dict[str, Any] = {
        "fixture_id": str(fixture_id) if isinstance(fixture_id, str) else fixture_id,
        "reason": reason,
    }
    if message:
        result["message"] = message
    if identities is None:
        return result
    result.update(
        {
            "status": status,
            "team_identities": identities,
            "schema_version": PREDICTION_COVERAGE_SCHEMA_VERSION,
        }
    )
    competition = row.get("competition_id")
    if isinstance(competition, str) and competition.strip():
        result["competition_id"] = competition
    for field in ("season", "kickoff_at", "home_team", "away_team"):
        value = row.get(field)
        if value is not None:
            result[field] = value
    return result


def prediction_coverage(
    candidates: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the auditable count invariant for one prediction projection."""

    candidate_ids: set[str] = set()
    for row in candidates:
        value = row.get("id")
        if isinstance(value, str) and value.strip():
            candidate_ids.add(value)
    prediction_ids: set[str] = set()
    for row in predictions:
        value = row.get("fixture_id")
        if isinstance(value, str) and value.strip():
            prediction_ids.add(value)
    blocked_ids: set[str] = set()
    for row in blocked:
        value = row.get("fixture_id")
        if isinstance(value, str) and value.strip():
            blocked_ids.add(value)
    covered_ids = prediction_ids | blocked_ids
    missing = sorted(candidate_ids - covered_ids)
    return {
        "schema_version": PREDICTION_COVERAGE_SCHEMA_VERSION,
        "candidate_count": len(candidate_ids),
        "prediction_count": len(predictions),
        "blocked_count": len(blocked),
        "complete": (
            not missing
            and len(predictions) + len(blocked) == len(candidate_ids)
            and len(covered_ids) == len(candidate_ids)
        ),
        "missing_fixture_ids": missing,
    }


__all__ = [
    "CURRENT_FIXTURE_FEED_KEY",
    "LEGACY_FIXTURE_FEED_KEY",
    "MULTI_SOURCE_FIXTURE_PROVIDER",
    "PREDICTION_COVERAGE_SCHEMA_VERSION",
    "blocked_fixture_record",
    "fixture_feed_key",
    "fixture_feed_section",
    "fixture_team_identities",
    "fixture_rows",
    "prediction_coverage",
    "snapshot_fixture_rows",
    "upcoming_fixture_rows",
    "apply_official_schedule_overlay",
    "merge_current_fixture_feeds",
]
