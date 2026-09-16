"""Exact, time-bounded joins for structured Crawl4AI fixture cards.

The browser edge deliberately produces *claims* rather than model features.
This module reconciles explicitly declared WhoScored, Premier League, LaLiga,
and Serie A public-page parser contracts with the canonical ESPN fixture universe.
Only an exact home/away/date/time match is accepted. The resulting records
remain comparison/display evidence until source terms and model admission
are explicitly approved.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


WHO_SCORED_TIMEZONE = ZoneInfo("Europe/London")
MAX_KICKOFF_DELTA_SECONDS = 15 * 60

# Crawl4AI is the shared fetch/render runtime.  These are the only page
# contracts that currently have enough structure for a conservative fixture
# join.  The parser contract, not the browser, decides the competition and
# time semantics.  Serie A is admitted only when its source-declared UTC
# ``matchDateUtc`` was extracted; cards without that field still fail closed.
PUBLIC_PAGE_CONTRACTS = frozenset(
    {
        "whoscored_public_fixtures_v1",
        "premier_league_public_fixtures_v1",
        "laliga_public_sports_events_v1",
        "seriea_public_fixtures_v1",
        "sports_event_jsonld_v1",
    }
)

# Keep the join-side identity contract in sync with the fetch-side parser
# declarations.  The browser is a shared execution layer; a parser contract
# is meaningful only when the page's declared source owns that contract.
PUBLIC_SOURCE_PARSER_CONTRACTS: dict[str, frozenset[str]] = {
    "whoscored_public_pages": frozenset({"auto", "whoscored_public_fixtures_v1"}),
    "premier_league_public_pages": frozenset(
        {"auto", "premier_league_public_fixtures_v1", "sports_event_jsonld_v1"}
    ),
    "laliga_public_pages": frozenset(
        {"auto", "laliga_public_sports_events_v1", "sports_event_jsonld_v1"}
    ),
    "laliga_official_news": frozenset({"auto", "official_news_jsonld_v1"}),
    "bundesliga_public_pages": frozenset({"auto", "sports_event_jsonld_v1"}),
    "seriea_public_pages": frozenset(
        {"auto", "seriea_public_fixtures_v1", "sports_event_jsonld_v1"}
    ),
    "ligue1_official_news": frozenset({"auto", "official_news_cards_v1"}),
}

# A page can carry a syntactically valid parser while being served from a
# different provider (for example, an allowlisted redirect or copied JSON-LD
# card).  Keep the join-side host ownership table explicit and bounded, just
# like the fetch-side admission contract.  Missing URLs remain compatible
# with legacy snapshots; any URL that is present must be HTTPS and source
# owned before a fixture join is attempted.
PUBLIC_SOURCE_HOSTS: dict[str, frozenset[str]] = {
    "whoscored_public_pages": frozenset({"www.whoscored.com", "whoscored.com"}),
    "premier_league_public_pages": frozenset({"www.premierleague.com", "premierleague.com"}),
    "laliga_public_pages": frozenset({"www.laliga.com", "laliga.com"}),
    "laliga_official_news": frozenset({"www.laliga.com", "laliga.com"}),
    "bundesliga_public_pages": frozenset({"www.bundesliga.com", "bundesliga.com"}),
    "seriea_public_pages": frozenset({"en.legaseriea.it", "www.legaseriea.it"}),
    "ligue1_official_news": frozenset({"www.ligue1.com", "ligue1.com"}),
}

# These are source/canonical naming variants observed in the public fixture
# cards.  Keep the table explicit: broad token stripping could join two
# different clubs (for example Manchester City and Manchester United).
_TEAM_ALIASES = {
    "arsenal": "arsenal",
    "afc bournemouth": "bournemouth",
    "bournemouth": "bournemouth",
    # ``_clean_team`` turns ``&`` into ``and`` before lookup.
    "brighton and hove albion": "brighton",
    "brighton": "brighton",
    "brentford": "brentford",
    "coventry": "coventry city",
    "coventry city": "coventry city",
    "crystal palace": "crystal palace",
    "chelsea": "chelsea",
    "everton": "everton",
    "fulham": "fulham",
    "hull": "hull city",
    "hull city": "hull city",
    "ipswich": "ipswich town",
    "ipswich town": "ipswich town",
    "leeds": "leeds united",
    "leeds united": "leeds united",
    "liverpool": "liverpool",
    "manchester city": "manchester city",
    "manchester united": "manchester united",
    "newcastle": "newcastle united",
    "newcastle united": "newcastle united",
    "nottingham forest": "nottingham forest",
    "sunderland": "sunderland",
    "tottenham": "tottenham hotspur",
    "tottenham hotspur": "tottenham hotspur",
    "aston villa": "aston villa",
    # Bundesliga JSON-LD uses German display names while ESPN uses shorter
    # provider names. Keep these aliases explicit; broad token stripping could
    # silently merge different clubs.
    "fc bayern munchen": "bayern munich",
    "bayern munich": "bayern munich",
    "vfb stuttgart": "vfb stuttgart",
    "sv elversberg": "sv elversberg",
    "bayer 04 leverkusen": "bayer leverkusen",
    "bayer leverkusen": "bayer leverkusen",
    "1 fc koln": "fc cologne",
    "fc cologne": "fc cologne",
    "tsg hoffenheim": "tsg hoffenheim",
    "rb leipzig": "rb leipzig",
    "borussia monchengladbach": "borussia monchengladbach",
    "borussia dortmund": "borussia dortmund",
    "hamburg sv": "hamburg sv",
    "sc freiburg": "sc freiburg",
    "werder bremen": "werder bremen",
    "fc augsburg": "fc augsburg",
    "schalke 04": "schalke 04",
    "mainz": "mainz",
    "mainz 05": "mainz",
    "1 fsv mainz 05": "mainz",
    "paderborn 07": "paderborn 07",
    "sc paderborn 07": "paderborn 07",
    "1 fc union berlin": "1 fc union berlin",
    "union berlin": "1 fc union berlin",
    "eintracht frankfurt": "eintracht frankfurt",
    "hamburger sv": "hamburg sv",
    "sport club freiburg": "sc freiburg",
    "sv werder bremen": "werder bremen",
    "fc schalke 04": "schalke 04",
}


def _source_team_key(value: object, *, parser_contract: str) -> str:
    """Return a conservative parser-scoped team key.

    The fallback is intentionally the normalized full name.  Only aliases
    already reviewed for the relevant public page contract are collapsed;
    unknown names stay distinct so a new naming convention cannot silently
    merge two clubs.
    """

    cleaned = _clean_team(value)
    if parser_contract in {
        "whoscored_public_fixtures_v1",
        "premier_league_public_fixtures_v1",
        "sports_event_jsonld_v1",
    }:
        return _TEAM_ALIASES.get(cleaned, cleaned)
    if parser_contract == "seriea_public_fixtures_v1":
        return {
            "inter": "internazionale",
            "inter milan": "internazionale",
            "internazionale": "internazionale",
            "milan": "ac milan",
            "ac milan": "ac milan",
            "roma": "as roma",
            "as roma": "as roma",
        }.get(cleaned, cleaned)
    # LaLiga JSON-LD normally carries the same canonical display name as the
    # ESPN fixture.  Keep an explicit set of observed provider names; broad
    # token stripping is intentionally avoided because it could merge clubs.
    if parser_contract == "laliga_public_sports_events_v1":
        return {
            "atletico de madrid": "atletico madrid",
            "atletico madrid": "atletico madrid",
            "malaga cf": "malaga",
            "malaga": "malaga",
            "valencia cf": "valencia",
            "valencia": "valencia",
            "fc barcelona": "barcelona",
            "barcelona": "barcelona",
            "ca osasuna": "osasuna",
            "osasuna": "osasuna",
            "rc celta": "celta",
            "celta": "celta",
            "celta vigo": "celta",
            "deportivo alaves": "alaves",
            "alaves": "alaves",
            "getafe cf": "getafe",
            "getafe": "getafe",
            "sevilla fc": "sevilla",
            "sevilla": "sevilla",
            "r racing club": "racing santander",
            "racing santander": "racing santander",
            "racing club": "racing santander",
            "villarreal cf": "villarreal",
            "villarreal": "villarreal",
            "rcd espanyol de barcelona": "espanyol",
            "rcd espanyol": "espanyol",
            "espanyol": "espanyol",
            "levante ud": "levante",
            "levante": "levante",
            "rc deportivo": "deportivo",
            "deportivo": "deportivo",
            "elche cf": "elche",
            "elche": "elche",
        }.get(cleaned, cleaned)
    # Preserve a conservative fallback for callers using an older or
    # unlisted parser contract; unknown names remain unchanged.
    return {
        "rc celta": "celta",
        "celta": "celta",
        "r c celta": "celta",
        "rcd espanyol": "espanyol",
        "rcd español": "espanyol",
        "espanyol": "espanyol",
    }.get(cleaned, cleaned)


def _clean_team(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("&", " and ").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def canonical_team_key(value: object) -> str:
    """Return an explicit, conservative club key for the supported league."""

    cleaned = _clean_team(value)
    return _TEAM_ALIASES.get(cleaned, cleaned)


def _record_kickoff(
    record: Mapping[str, Any], *, parser_contract: str
) -> datetime | None:
    """Parse the source-owned kickoff fields without guessing a timezone."""

    if parser_contract in {
        "laliga_public_sports_events_v1",
        "seriea_public_fixtures_v1",
        "sports_event_jsonld_v1",
    }:
        value = record.get("kickoff_at")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    return parse_whoscored_kickoff(
        record.get("date_label"),
        record.get("kickoff_time_label"),
        timezone_name="Europe/London",
    )


def _parse_display_month_day_time(
    date_label: object, time_label: object
) -> tuple[int, int, int, int] | None:
    """Parse a local display label whose year is intentionally omitted.

    The Premier League card currently renders labels such as ``Fri 21 Aug``
    and ``20:00``.  The year is not present in the public card, so this helper
    returns only month/day/hour/minute.  The caller must resolve the year from
    a unique exact canonical team-pair fixture; it may never use this as a
    name-only or nearest-match fallback.
    """

    if not isinstance(date_label, str) or not isinstance(time_label, str):
        return None
    date_text = " ".join(date_label.strip().split())
    time_text = " ".join(time_label.strip().split())
    if re.search(r"\b\d{4}\b", date_text) or not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
        return None
    date_text = re.sub(r"^[A-Za-z]+,?\s+", "", date_text)
    for date_format in ("%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(date_text, date_format)
            hour, minute = (int(value) for value in time_text.split(":"))
            return parsed.month, parsed.day, hour, minute
        except (TypeError, ValueError):
            continue
    return None


def parse_whoscored_kickoff(
    date_label: object,
    time_label: object,
    *,
    timezone_name: str = "Europe/London",
) -> datetime | None:
    """Parse the human-readable WhoScored date/time label into UTC.

    WhoScored does not expose a timezone in the card text.  The page is a
    Premier League page, so the source's competition timezone is used
    explicitly and retained in the returned join metadata.  A missing or
    malformed label fails closed instead of guessing UTC.
    """

    if not isinstance(date_label, str) or not isinstance(time_label, str):
        return None
    date_text = " ".join(date_label.strip().split())
    time_text = " ".join(time_label.strip().split())
    # Example: ``Friday, Aug 21 2026``.  Do not accept a date-only value.
    date_text = re.sub(r"^[A-Za-z]+,\s*", "", date_text)
    if not date_text or not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
        return None
    parsed: datetime | None = None
    for date_format in ("%b %d %Y", "%B %d %Y"):
        try:
            parsed = datetime.strptime(f"{date_text} {time_text}", f"{date_format} %H:%M")
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        return None
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def _parse_fixture_kickoff(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def classify_public_page_admission(
    *,
    parser_contract: str,
    join_status: str,
    pre_match_eligible: bool | None,
    observed_at: object,
    canonical_kickoff_at: object,
    source_license_status: str = "unknown",
) -> dict[str, Any]:
    """Explain why a joined page is, or is not, a model candidate.

    Crawl4AI only supplies the fetch/render execution layer. This diagnostic
    is deliberately advisory and fail-closed: even an exact, pre-kickoff join
    with an open source policy remains ``candidate_ready`` until a dedicated
    source adapter and the model's own conflict/effective-time gates admit it.
    """

    parser_supported = parser_contract in PUBLIC_PAGE_CONTRACTS
    exact_fixture_join = join_status == "exact"
    canonical_kickoff = _parse_fixture_kickoff(canonical_kickoff_at)
    observed = _parse_fixture_kickoff(observed_at)
    observed_before_kickoff = (
        observed is not None
        and canonical_kickoff is not None
        and observed <= canonical_kickoff
    )
    license_status = str(source_license_status or "unknown").strip().lower()
    checks = {
        "parser_supported": parser_supported,
        "exact_fixture_join": exact_fixture_join,
        "pre_match": pre_match_eligible is True,
        "observed_before_kickoff": observed_before_kickoff,
        "license_reviewed": license_status in {"open", "authorized", "restricted", "denied"},
    }

    if not parser_supported:
        status, reason, action = (
            "blocked",
            "parser_contract_unsupported",
            "declare_and_test_a_source_specific_parser_contract",
        )
    elif not exact_fixture_join:
        status, reason, action = (
            "blocked",
            "fixture_join_not_exact",
            "retain_in_quarantine_until_home_away_and_kickoff_match_exactly",
        )
    elif pre_match_eligible is not True:
        status, reason, action = (
            "blocked",
            "fixture_not_upcoming",
            "exclude_after_kickoff_or_when_fixture_status_is_unknown",
        )
    elif canonical_kickoff is None:
        status, reason, action = (
            "blocked",
            "canonical_kickoff_missing_or_invalid",
            "repair_canonical_fixture_time_before_review",
        )
    elif observed is None:
        status, reason, action = (
            "blocked",
            "observed_at_missing_or_invalid",
            "record_first_observed_time_before_any_model_review",
        )
    elif observed > canonical_kickoff:
        status, reason, action = (
            "blocked",
            "observed_after_kickoff",
            "exclude_from_pre_match_features_and_keep_as_post_kickoff_audit",
        )
    elif license_status in {"restricted", "denied"}:
        status, reason, action = (
            "blocked",
            "source_license_restricted_or_denied",
            "keep_display_only_and_resolve_source_terms",
        )
    elif license_status not in {"open", "authorized"}:
        status, reason, action = (
            "review_required",
            "source_license_review_required",
            "review_terms_and_time_semantics_before_adapter_admission",
        )
    else:
        status, reason, action = (
            "candidate_ready",
            "dedicated_source_model_adapter_required",
            "run_source_specific_fact_conflict_and_effective_time_gates",
        )

    return {
        "status": status,
        "reason_code": reason,
        "next_action": action,
        "enters_model": False,
        "source_license_status": license_status,
        "checks": checks,
    }


def _candidate_fixtures(
    record: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
    *,
    parser_contract: str = "whoscored_public_fixtures_v1",
) -> tuple[datetime | None, list[tuple[Mapping[str, Any], float]]]:
    source_kickoff = _record_kickoff(record, parser_contract=parser_contract)
    source_home = _source_team_key(record.get("home_team"), parser_contract=parser_contract)
    source_away = _source_team_key(record.get("away_team"), parser_contract=parser_contract)
    if not source_home or not source_away:
        return source_kickoff, []
    short_display = None
    if source_kickoff is None and not re.search(r"\b\d{4}\b", str(record.get("date_label") or "")):
        short_display = _parse_display_month_day_time(
            record.get("date_label"), record.get("kickoff_time_label")
        )
        if short_display is None:
            return None, []
    # A source-specific parser may omit or reject its kickoff field while
    # still returning a team card. Keep that card as an explicit join
    # diagnostic; never subtract a datetime from ``None`` and abort the
    # entire multi-source sync.
    if source_kickoff is None and short_display is None:
        return None, []
    candidates: list[tuple[Mapping[str, Any], float]] = []
    for fixture in fixtures:
        if not isinstance(fixture, Mapping):
            continue
        expected_competition = {
            "laliga_public_sports_events_v1": "la-liga",
            "seriea_public_fixtures_v1": "serie-a",
            "sports_event_jsonld_v1": "bundesliga",
        }.get(parser_contract, "premier-league")
        if fixture.get("competition_id") != expected_competition:
            continue
        home = _source_team_key(fixture.get("home_team"), parser_contract=parser_contract)
        away = _source_team_key(fixture.get("away_team"), parser_contract=parser_contract)
        if home != source_home or away != source_away:
            continue
        kickoff = _parse_fixture_kickoff(fixture.get("kickoff_at"))
        if kickoff is None:
            continue
        if short_display is not None:
            local = kickoff.astimezone(WHO_SCORED_TIMEZONE)
            if (local.month, local.day, local.hour, local.minute) != short_display:
                continue
            candidates.append((fixture, 0.0))
            continue
        # ``source_kickoff`` is narrowed by the early guard above for the
        # full-datetime path; keep the explicit check here so static type
        # checkers do not treat the optional value as subtractable.
        if source_kickoff is None:
            continue
        delta = abs((kickoff - source_kickoff).total_seconds())
        if delta <= MAX_KICKOFF_DELTA_SECONDS:
            candidates.append((fixture, delta))
    if short_display is not None:
        if len(candidates) != 1:
            return None, []
        return _parse_fixture_kickoff(candidates[0][0].get("kickoff_at")), candidates
    return source_kickoff, candidates


def join_public_page_records(
    records: Sequence[Mapping[str, Any]],
    fixtures: Sequence[Mapping[str, Any]],
    *,
    parser_contract: str,
    source_license_status: str = "unknown",
    observed_at: object = None,
) -> dict[str, Any]:
    """Join one declared public-page contract to canonical fixtures.

    Every input card receives either an exact join or a bounded diagnostic.
    A joined record carries canonical IDs and source time semantics, but
    ``enters_model`` remains false because page capture and source licence
    review are separate gates.  This function never broadens a parser's
    competition or accepts a name-only join.
    """

    if parser_contract not in PUBLIC_PAGE_CONTRACTS:
        raise ValueError(f"unsupported public page join contract: {parser_contract}")

    joined: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            unmatched.append({"reason": "invalid_record"})
            continue
        record = deepcopy(dict(raw_record))
        source_kickoff, candidates = _candidate_fixtures(
            record,
            fixtures,
            parser_contract=parser_contract,
        )
        diagnostic_base = {
            "source_match_id": record.get("source_match_id"),
            "home_team": record.get("home_team"),
            "away_team": record.get("away_team"),
            "date_label": record.get("date_label"),
            "kickoff_time_label": record.get("kickoff_time_label"),
        }
        if source_kickoff is None:
            unmatched.append({**diagnostic_base, "reason": "source_kickoff_unparseable"})
            continue
        if not candidates:
            unmatched.append({
                **diagnostic_base,
                "reason": "no_exact_team_and_kickoff_match",
                "source_kickoff_at": source_kickoff.isoformat(),
            })
            continue
        candidates.sort(key=lambda item: (item[1], str(item[0].get("id"))))
        best_delta = candidates[0][1]
        best = [item for item in candidates if item[1] == best_delta]
        if len(best) != 1:
            unmatched.append({
                **diagnostic_base,
                "reason": "ambiguous_team_and_kickoff_match",
                "candidate_fixture_ids": [item[0].get("id") for item in best],
                "source_kickoff_at": source_kickoff.isoformat(),
            })
            continue
        fixture, delta = best[0]
        canonical_kickoff = _parse_fixture_kickoff(fixture.get("kickoff_at"))
        if canonical_kickoff is None or not fixture.get("id"):
            unmatched.append({**diagnostic_base, "reason": "canonical_fixture_missing_id_or_kickoff"})
            continue
        admission = classify_public_page_admission(
            parser_contract=parser_contract,
            join_status="exact",
            pre_match_eligible=fixture.get("status") == "upcoming",
            observed_at=observed_at if observed_at is not None else record.get("observed_at"),
            canonical_kickoff_at=canonical_kickoff.isoformat(),
            source_license_status=source_license_status,
        )
        joined.append(
            {
                **record,
                "source_parser": parser_contract,
                "fixture_id": str(fixture["id"]),
                "competition_id": fixture.get("competition_id"),
                "canonical_home_team": fixture.get("home_team"),
                "canonical_away_team": fixture.get("away_team"),
                "source_kickoff_at": source_kickoff.isoformat(),
                "canonical_kickoff_at": canonical_kickoff.isoformat(),
                "kickoff_delta_seconds": int(delta),
                "source_time_resolution": (
                    "year_resolved_from_unique_canonical_pair"
                    if _record_kickoff(record, parser_contract=parser_contract) is None
                    else "full_source_datetime"
                ),
                "source_timezone": (
                    "embedded_iso8601"
                    if parser_contract in {
                        "laliga_public_sports_events_v1",
                        "seriea_public_fixtures_v1",
                        "sports_event_jsonld_v1",
                    }
                    else "Europe/London"
                ),
                "join_status": "exact",
                "join_confidence": 0.99,
                "pre_match_eligible": fixture.get("status") == "upcoming",
                "enters_model": False,
                "fact_type": (
                    "market_comparison"
                    if isinstance(record.get("one_x_two_odds"), Mapping)
                    else "fixture_context"
                ),
                "model_exclusion_reason": (
                    "public_source_market_comparison_requires_license_review"
                    if isinstance(record.get("one_x_two_odds"), Mapping)
                    else "public_page_fixture_context_requires_source_and_time_review"
                ) + f"; admission={admission['reason_code']}",
                "admission": admission,
            }
        )
    status = "exact" if joined and not unmatched else "partial" if joined else "unavailable"
    admission_counts: dict[str, int] = {}
    for row in joined:
        admission_value = row.get("admission")
        if not isinstance(admission_value, Mapping):
            continue
        state = str(admission_value.get("status") or "blocked")
        admission_counts[state] = admission_counts.get(state, 0) + 1
    return {
        "joined": joined,
        "unmatched": unmatched,
        "summary": {
            "status": status,
            "parser": parser_contract,
            "record_count": len(records),
            "joined_count": len(joined),
            "unmatched_count": len(unmatched),
            "max_kickoff_delta_seconds": MAX_KICKOFF_DELTA_SECONDS,
            "timezone": (
                "embedded_iso8601"
                if parser_contract in {
                    "laliga_public_sports_events_v1",
                    "seriea_public_fixtures_v1",
                    "sports_event_jsonld_v1",
                }
                else "Europe/London"
            ),
            "model_admission": "display_only_until_source_license_and_time_review",
            "admission_counts": admission_counts,
        },
    }


def join_whoscored_records(
    records: Sequence[Mapping[str, Any]],
    fixtures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Backward-compatible WhoScored-specific join wrapper."""

    return join_public_page_records(
        records,
        fixtures,
        parser_contract="whoscored_public_fixtures_v1",
    )


def _page_identity_failure(
    page: dict[str, Any],
    *,
    reason: str,
    parser_contract: object = None,
) -> dict[str, Any]:
    """Attach a bounded fail-closed join diagnostic to one page."""

    records = page.get("extracted_records")
    record_count = len(records) if isinstance(records, list) else 0
    # Keep one diagnostic even when the parser emitted no records: otherwise a
    # mismatched declaration would be indistinguishable from an unconfigured
    # page in downstream source-health reports.
    unmatched = [
        {"reason": reason, "enters_model": False}
        for _ in range(record_count or 1)
    ]
    summary: dict[str, Any] = {
        "status": "unavailable",
        "parser": parser_contract,
        "record_count": record_count,
        "joined_count": 0,
        "unmatched_count": len(unmatched),
        "reason": reason,
        "model_admission": "display_only_until_source_license_and_time_review",
        "admission_counts": {},
    }
    page["joined_fixture_records"] = []
    page["unmatched_fixture_records"] = unmatched
    page["fixture_join"] = summary
    return summary


def _page_parser_identity(
    page: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Return source ID, declared parser, and resolved parser for a page."""

    raw_source = page.get("source_id")
    raw_fact_source = page.get("fact_source_id")
    source_id = str(raw_source or raw_fact_source or "").strip() or None
    extraction = page.get("extraction")
    resolved = extraction.get("parser") if isinstance(extraction, Mapping) else None
    declared = page.get("parser_contract")
    declared_value = str(declared).strip() if isinstance(declared, str) and declared.strip() else None
    resolved_value = str(resolved).strip() if isinstance(resolved, str) and resolved.strip() else None
    return source_id, declared_value, resolved_value


def _page_identity_mismatch(page: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Validate source/parser fields before attempting a fixture join.

    Legacy snapshots without any source ID remain joinable for backwards
    compatibility, but once an identity is declared every parser field must be
    internally consistent and owned by that source's contract.
    """

    raw_source = page.get("source_id")
    raw_fact_source = page.get("fact_source_id")
    source_id, declared, resolved = _page_parser_identity(page)
    if raw_source not in (None, "") and raw_fact_source not in (None, ""):
        if str(raw_source).strip() != str(raw_fact_source).strip():
            return "source_identity_mismatch", source_id
    if declared is not None and resolved is not None and declared != resolved:
        # ``auto`` is expected to differ from the resolved parser, so it is a
        # routing declaration rather than a conflict.
        if declared != "auto":
            return "parser_contract_mismatch", source_id
    effective = resolved or declared
    if effective is None:
        return ("parser_contract_missing", source_id) if source_id else (None, source_id)
    if effective not in PUBLIC_PAGE_CONTRACTS:
        return "parser_contract_unsupported", source_id
    if source_id:
        allowed = PUBLIC_SOURCE_PARSER_CONTRACTS.get(source_id)
        if allowed is None:
            return "parser_source_mismatch", source_id
        # An ``auto`` declaration may resolve to a source-owned contract; the
        # resolved parser is the identity-bearing value when available.
        if effective not in allowed:
            return "parser_source_mismatch", source_id
        expected_hosts = PUBLIC_SOURCE_HOSTS.get(source_id)
        if expected_hosts:
            # Validate both the requested and resolved URL when present.  A
            # wrong final host is enough to quarantine the page even if the
            # parser/source fields look internally consistent.
            urls = [page.get("url")]
            if page.get("final_url") not in (None, ""):
                urls.append(page.get("final_url"))
            for raw_url in urls:
                if raw_url in (None, ""):
                    continue
                if not isinstance(raw_url, str):
                    return "source_host_mismatch", source_id
                parsed = urlparse(raw_url)
                host = (parsed.hostname or "").lower().rstrip(".")
                if parsed.scheme != "https" or host not in expected_hosts:
                    return "source_host_mismatch", source_id
    return None, source_id


def attach_crawl4ai_fixture_joins(
    crawl_snapshot: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Annotate all supported Crawl4AI page contracts with exact joins.

    Crawl4AI remains the execution layer.  Each page is routed by its own
    declared parser contract, and unsupported/generic pages are retained as
    raw evidence without an invented fixture association.
    """

    result = deepcopy(dict(crawl_snapshot))
    pages = result.get("pages")
    if not isinstance(pages, list):
        result["fixture_join"] = {"status": "unavailable", "joined_count": 0, "unmatched_count": 0}
        return result
    aggregate_joined = 0
    aggregate_unmatched = 0
    page_summaries: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        identity_reason, source_id = _page_identity_mismatch(page)
        if identity_reason is not None:
            summary = _page_identity_failure(
                page,
                reason=identity_reason,
                parser_contract=(
                    page.get("extraction", {}).get("parser")
                    if isinstance(page.get("extraction"), Mapping)
                    else page.get("parser_contract")
                ),
            )
            summary["source_id"] = source_id
            page_summaries.append({"url": page.get("url"), **summary})
            aggregate_unmatched += int(summary["unmatched_count"])
            continue
        extraction = page.get("extraction")
        records = page.get("extracted_records")
        parser_contract = (
            extraction.get("parser")
            if isinstance(extraction, Mapping)
            else page.get("parser_contract")
        )
        if parser_contract not in PUBLIC_PAGE_CONTRACTS:
            continue
        if not isinstance(records, list):
            records = []
        join = join_public_page_records(
            records,
            fixtures,
            parser_contract=str(parser_contract),
            source_license_status=(
                page.get("policy", {}).get("license_status", page.get("license_status", "unknown"))
                if isinstance(page.get("policy"), Mapping)
                else page.get("license_status", "unknown")
            ),
            observed_at=page.get("observed_at"),
        )
        page["joined_fixture_records"] = join["joined"]
        page["unmatched_fixture_records"] = join["unmatched"]
        page["fixture_join"] = join["summary"]
        page_summaries.append({
            "url": page.get("url"),
            "source_id": source_id,
            **join["summary"],
        })
        aggregate_joined += len(join["joined"])
        aggregate_unmatched += len(join["unmatched"])
    result["fixture_join"] = {
        "status": "exact" if aggregate_joined and not aggregate_unmatched else "partial" if aggregate_joined else "unavailable",
        "joined_count": aggregate_joined,
        "unmatched_count": aggregate_unmatched,
        "pages": page_summaries,
        "model_admission": "display_only_until_source_license_and_time_review",
        "admission_counts": {
            status: sum(
                int(page.get("admission_counts", {}).get(status) or 0)
                for page in page_summaries
                if isinstance(page.get("admission_counts"), Mapping)
            )
            for status in {
                status
                for page in page_summaries
                if isinstance(page.get("admission_counts"), Mapping)
                for status in page["admission_counts"]
            }
        },
    }
    return result


def attach_whoscored_fixture_joins(
    crawl_snapshot: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Backward-compatible alias for callers that only expect WhoScored."""

    return attach_crawl4ai_fixture_joins(crawl_snapshot, fixtures)


__all__ = [
    "MAX_KICKOFF_DELTA_SECONDS",
    "PUBLIC_PAGE_CONTRACTS",
    "WHO_SCORED_TIMEZONE",
    "attach_crawl4ai_fixture_joins",
    "attach_whoscored_fixture_joins",
    "canonical_team_key",
    "classify_public_page_admission",
    "join_public_page_records",
    "join_whoscored_records",
    "parse_whoscored_kickoff",
]
