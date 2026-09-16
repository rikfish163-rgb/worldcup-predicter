"""Convert validated live-source snapshots into immutable intelligence records."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from league_platform.fixture_feed import fixture_rows
from league_platform.intelligence import Observation, ObservationLedger, observation_from_payload
from league_platform.player_evidence import snapshot_player_observations


DISPLAY_ONLY_REASONS = {
    "roster_evidence": "provider_roster_not_confirmed_lineup",
    "sports_lottery_markets": "official_lottery_fixture_join_unproven",
    "independent_asian_market": "independent_market_fixture_join_unproven",
    "news_fact_candidate": "news_fact_not_structured_model_feature",
    "lineup": "provider_reported_lineup_not_authoritative",
    "injury": "provider_reported_injury_not_authoritative",
    "match_result": "postmatch_result_not_a_pre_match_feature",
    "match_status": "live_or_postmatch_status",
    "player_roster": "player_identity_not_canonical",
    "player_lineup": "player_identity_not_canonical",
}


# Crawl4AI is an execution layer, so these adapter-owned fields must survive
# the snapshot-to-ledger projection.  The publisher re-validates the same
# envelope immediately before a write; silently dropping a false/unknown
# value here would turn missing evidence into an identity or runtime error
# that cannot be audited later.
_CRAWL4AI_PROVENANCE_FIELDS = (
    "source",
    "source_id",
    "fact_source_id",
    "provider",
    "provider_role",
    "capture_engine",
    "capture_role",
    "source_role",
    "parser_contract",
    "runtime_evidence",
    "network_opened",
    "test_only",
    "model_eligible",
    "enters_model",
    "requested_model_eligible",
    "model_exclusion_reason",
    "policy",
    "license_status",
    "rights_reference",
    "robots",
)


def _crawl4ai_provenance_payload(page: dict) -> dict[str, object]:
    """Copy the bounded adapter/runtime envelope into an observation payload."""

    return {key: page.get(key) for key in _CRAWL4AI_PROVENANCE_FIELDS}


def _timestamp(value: str | None, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _raw_hash(source: dict) -> str | None:
    """Prefer the captured response hash for provenance records."""

    for key in ("raw_sha256", "content_sha256"):
        value = source.get(key)
        if not isinstance(value, str) or len(value) != 64:
            continue
        try:
            int(value, 16)
        except ValueError:
            continue
        return value.lower()
    return None


def _model_entry_decision(source: dict, requested: bool) -> tuple[bool, tuple[str, ...]]:
    """Apply the source-level policy boundary before an observation enters a model.

    Adapters are allowed to remain permissive for audit/display purposes, but
    a source can explicitly deny AI training, model use, or access under its
    robots policy.  Those denials must win over a caller's ``requested=True``
    flag.  Missing policy metadata is intentionally treated as unknown rather
    than as an implicit denial so existing authorized feeds remain compatible;
    source audits can still quarantine unknown sources separately.
    """

    if not requested:
        return False, ()
    if not isinstance(source, dict):
        return False, ("missing_source_policy",)

    policy = source.get("policy")
    records = [source]
    if isinstance(policy, dict):
        records.append(policy)

    reasons: list[str] = []
    false_keys = {
        "model_eligible": "model_eligibility_denied",
        "allow_model": "model_use_denied",
        "ai_train_allowed": "ai_training_denied",
        "robots_allowed": "robots_denied",
        "robots_allowed_for_model": "robots_model_use_denied",
    }
    true_denial_keys = {
        "paid": "paid_source",
        "commercial": "commercial_source",
        "requires_login": "login_required",
        "login_required": "login_required",
        "captcha_required": "captcha_required",
    }
    for record in records:
        for key, reason in false_keys.items():
            if record.get(key) is False and reason not in reasons:
                reasons.append(reason)
        for key, reason in true_denial_keys.items():
            if record.get(key) is True and reason not in reasons:
                reasons.append(reason)
        for key in ("content_signal", "content-signal", "contentSignal", "robots_signal"):
            value = record.get(key)
            if not isinstance(value, str):
                continue
            normalized = value.lower().replace("_", "-").replace(" ", "")
            if "ai-train=no" in normalized or "ai-train=0" in normalized:
                if "ai_training_denied" not in reasons:
                    reasons.append("ai_training_denied")
            if "robots=disallow" in normalized or "robots-denied" in normalized:
                if "robots_denied" not in reasons:
                    reasons.append("robots_denied")
    return not reasons, tuple(reasons)


def _model_entry_fields(source: dict, requested: bool) -> dict[str, object]:
    """Return the immutable model-entry decision and any quarantine reason."""

    allowed, reasons = _model_entry_decision(source, requested)
    return {
        "enters_model": allowed,
        "model_exclusion_reason": ";".join(sorted(reasons)) if reasons else None,
    }


def snapshot_observations(snapshot: dict) -> list[Observation]:
    """Create provenance-rich observations without deriving future features."""

    fallback = snapshot.get("as_of") or datetime.now(timezone.utc).isoformat()
    rows: list[Observation] = []
    for fixture in fixture_rows(snapshot):
        source = fixture.get("source", {})
        if not source.get("url") or not source.get("raw_sha256"):
            continue
        score = fixture.get("score")
        result_scope = fixture.get("result_scope")
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(fixture["id"]),
                kind="fixture_schedule",
                payload={key: fixture.get(key) for key in ("competition_id", "season", "kickoff_at", "home_team", "away_team", "status", "score", "result_scope")},
                source_name=str(source.get("name", "unknown_fixture_provider")),
                source_url=source["url"],
                source_tier="authorized",
                observed_at=_timestamp(source.get("retrieved_at"), fallback),
                confidence=0.98,
                **_model_entry_fields(source, True),
                raw_hash=_raw_hash(source),
            )
        )
        # A schedule row carries the current status for identity registration,
        # but the live desk also needs an immutable, display-only status
        # observation with the latest score/clock/period.  Keeping this as a
        # separate kind avoids teaching a pre-match consumer to interpret a
        # mutable fixture status as an event stream.
        if fixture.get("status") in {"live", "finished", "postponed", "cancelled"}:
            status_payload = {
                "status": fixture.get("status"),
                "score": fixture.get("score"),
                "halftime_score": fixture.get("halftime_score"),
                "result_scope": fixture.get("result_scope"),
                "status_observed_at": fixture.get("status_observed_at"),
                "clock": fixture.get("clock"),
                "period": fixture.get("period"),
                "status_text": fixture.get("status_text"),
                "kickoff_at": fixture.get("kickoff_at"),
            }
            rows.append(
                observation_from_payload(
                    entity_type="fixture",
                    entity_id=str(fixture["id"]),
                    kind="match_status",
                    payload=status_payload,
                    source_name=str(source.get("name", "ESPN")),
                    source_url=source["url"],
                    source_tier="reliable_public_provider",
                    observed_at=_timestamp(
                        fixture.get("status_observed_at") or source.get("retrieved_at"),
                        fallback,
                    ),
                    confidence=0.98,
                    enters_model=False,
                    model_exclusion_reason=DISPLAY_ONLY_REASONS["match_status"],
                    raw_hash=_raw_hash(source),
                )
            )
        # Keep a separate immutable post-match observation so Sites can build
        # the canonical result projection and later score frozen predictions.
        # The explicit result scope prevents an extra-time or penalty score
        # from being mistaken for Matchline's 90-minute settlement target.
        if (
            fixture.get("status") == "finished"
            and result_scope in {"regulation_90", "extra_time", "penalties"}
            and isinstance(score, dict)
            and all(
                isinstance(score.get(key), int)
                and not isinstance(score.get(key), bool)
                and score[key] >= 0
                for key in ("home", "away")
            )
        ):
            rows.append(
                observation_from_payload(
                    entity_type="fixture",
                    entity_id=str(fixture["id"]),
                    kind="match_result",
                    payload={
                        "status": "finished",
                        "score": {"home": score["home"], "away": score["away"]},
                        "halftime_score": fixture.get("halftime_score"),
                        "score_scope": result_scope,
                        "kickoff_at": fixture.get("kickoff_at"),
                    },
                    source_name=str(source.get("name", "ESPN")),
                    source_url=source["url"],
                    source_tier="authorized",
                    observed_at=_timestamp(source.get("retrieved_at"), fallback),
                    confidence=0.98,
                    enters_model=False,
                    model_exclusion_reason=DISPLAY_ONLY_REASONS["match_result"],
                    raw_hash=_raw_hash(source),
                )
            )
    for market in snapshot.get("espn_markets", {}).get("markets", []):
        source = market.get("source", {})
        if not source.get("url") or not source.get("raw_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(market["fixture_id"]),
                kind="market_1x2",
                payload={key: market.get(key) for key in ("provider", "american_odds", "probability", "retrieved_at")},
                source_name=str(source.get("name", "ESPN event summary")),
                source_url=source["url"],
                source_tier="authorized",
                observed_at=_timestamp(market.get("retrieved_at"), fallback),
                confidence=0.9,
                **_model_entry_fields(source, True),
                raw_hash=_raw_hash(source),
            )
        )
    for status in snapshot.get("espn_markets", {}).get("team_status", []):
        source = status.get("source", {})
        if not source.get("url") or not source.get("raw_sha256"):
            continue
        confirmed_lineup = (
            status.get("status") == "confirmed_lineup"
            and status.get("confirmed") is True
            and status.get("model_eligible") is True
        )
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(status["fixture_id"]),
                kind="lineup" if confirmed_lineup else "roster_evidence",
                payload={
                    key: status.get(key)
                    for key in ("provider", "confirmed", "model_eligible", "status", "teams", "note")
                },
                source_name="ESPN event summary",
                source_url=source["url"],
                source_tier="reliable_public_provider",
                observed_at=_timestamp(status.get("retrieved_at"), fallback),
                confidence=0.65,
                enters_model=confirmed_lineup,
                model_exclusion_reason=(
                    None
                    if confirmed_lineup
                    else DISPLAY_ONLY_REASONS["roster_evidence"]
                ),
                raw_hash=_raw_hash(source),
            )
        )
    for roster in snapshot.get("espn_rosters", {}).get("rosters", []):
        if not isinstance(roster, dict):
            continue
        source = roster.get("source", {})
        provider_team_id = roster.get("provider_team_id")
        if (
            not isinstance(source, dict)
            or not provider_team_id
            or not source.get("url")
            or not source.get("raw_sha256")
        ):
            continue
        rows.append(
            observation_from_payload(
                entity_type="team",
                entity_id=f"espn:{provider_team_id}",
                kind="player_roster",
                payload={
                    key: roster.get(key)
                    for key in (
                        "competition_id",
                        "provider_team_id",
                        "team_name",
                        "team_abbreviation",
                        "season",
                        "provider_timestamp",
                        "scheduled_fixture_ids",
                        "scheduled_kickoffs",
                        "athletes",
                        "athlete_count",
                        "status",
                        "enters_model",
                        "model_eligible",
                        "model_exclusion_reason",
                    )
                },
                source_name="ESPN team roster",
                source_url=source["url"],
                source_tier="reliable_public_provider",
                observed_at=_timestamp(roster.get("retrieved_at"), fallback),
                confidence=0.65,
                enters_model=False,
                model_exclusion_reason="team_roster_not_fixture_specific_or_confirmed_lineup",
                raw_hash=_raw_hash(source),
            )
        )
    for incident in snapshot.get("espn_markets", {}).get("incidents", []):
        if not isinstance(incident, dict):
            continue
        source = incident.get("source", {})
        fixture_id = incident.get("fixture_id")
        if not isinstance(source, dict) or fixture_id in (None, "") or not source.get("url") or not source.get("raw_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(fixture_id),
                kind="live_event",
                payload={"events": incident.get("events", [])},
                source_name=str(source.get("name", "ESPN event summary")),
                source_url=source["url"],
                source_tier="reliable_public_provider",
                observed_at=_timestamp(incident.get("retrieved_at"), fallback),
                confidence=0.95,
                enters_model=False,
                model_exclusion_reason="live_or_postmatch_event",
                raw_hash=_raw_hash(source),
            )
        )
    for stats in snapshot.get("espn_markets", {}).get("match_stats", []):
        if not isinstance(stats, dict):
            continue
        source = stats.get("source", {})
        fixture_id = stats.get("fixture_id")
        if not isinstance(source, dict) or fixture_id in (None, "") or not source.get("url") or not source.get("raw_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(fixture_id),
                kind="match_stat",
                payload={"teams": stats.get("teams", {}), "players": stats.get("players", [])},
                source_name=str(source.get("name", "ESPN event summary")),
                source_url=source["url"],
                source_tier="reliable_public_provider",
                observed_at=_timestamp(stats.get("retrieved_at"), fallback),
                confidence=0.9,
                enters_model=False,
                model_exclusion_reason="live_or_postmatch_event",
                raw_hash=_raw_hash(source),
            )
        )
    for market in snapshot.get("sports_lottery", {}).get("matches", []):
        source = market.get("source", {})
        if not source.get("url") or not source.get("raw_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="lottery_match",
                entity_id=str(market.get("match_id") or market.get("match_num")),
                kind="sports_lottery_markets",
                payload={key: market.get(key) for key in ("match_num", "league", "home_team", "away_team", "kickoff_at", "had_odds", "had_probability", "hhad_line", "hhad_odds", "hhad_probability", "ttg_odds", "ttg_probability", "hafu_odds", "hafu_probability", "crs_odds", "crs_probability")},
                source_name="Sports Lottery",
                source_url=source["url"],
                source_tier="official",
                observed_at=_timestamp(source.get("retrieved_at"), fallback),
                confidence=0.95,
                # The raw lottery feed is not yet joined to a canonical ESPN
                # fixture with high-confidence entity and time semantics.
                # Keep it in the immutable ledger for purchase/display audit,
                # but never let an unmatched row become a model feature.
                enters_model=False,
                model_exclusion_reason=DISPLAY_ONLY_REASONS["sports_lottery_markets"],
                raw_hash=_raw_hash(source),
            )
        )
    for market in snapshot.get("oddstorm", {}).get("lines", []):
        source = market.get("source", {})
        if not source.get("url") or not source.get("raw_sha256") or not market.get("match_id"):
            continue
        observed_at = _timestamp(market.get("observed_at"), fallback)
        effective_at = market.get("effective_at")
        rows.append(
            observation_from_payload(
                entity_type="provider_fixture",
                entity_id=f"oddstorm:{market['match_id']}",
                kind="independent_asian_market",
                payload={
                    key: market.get(key)
                    for key in (
                        "home_team",
                        "away_team",
                        "kickoff_at",
                        "kickoff_time_quality",
                        "kickoff_time_basis",
                        "handicap",
                        "total",
                        "available_at",
                    )
                }
                | {
                    "source_model_eligible": bool(market.get("enters_model")),
                    "canonical_join_status": "unmatched",
                    "purchase_eligible": False,
                },
                source_name=str(source.get("name", "OddStorm public bookmaker comparison")),
                source_url=source["url"],
                source_tier="authorized",
                observed_at=observed_at,
                published_at=effective_at,
                confidence=0.75,
                # Provider-level time eligibility is necessary but not
                # sufficient.  Until entity reconciliation proves the exact
                # canonical and Sports-Lottery fixture, the ledger row stays
                # display/audit-only.
                enters_model=False,
                model_exclusion_reason=DISPLAY_ONLY_REASONS["independent_asian_market"],
                raw_hash=_raw_hash(source),
            )
        )
    for feature in snapshot.get("understat", {}).get("team_features", []):
        source = feature.get("source", {})
        if not source.get("url") or not source.get("content_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="team",
                entity_id=str(feature.get("provider_team_id")),
                kind="team_form_xg",
                payload={key: feature.get(key) for key in ("competition_id", "team", "sample_n", "last_match_at", "xg_for", "xg_against", "goals_for", "goals_against")},
                source_name="Understat",
                source_url=source["url"],
                source_tier="authorized",
                observed_at=_timestamp(source.get("retrieved_at"), fallback),
                confidence=0.85,
                **_model_entry_fields(source, True),
                raw_hash=_raw_hash(source),
            )
        )
    for feed in snapshot.get("news", {}).get("feeds", []):
        feed_source_url = feed.get("url")
        if not feed_source_url or not feed.get("raw_sha256"):
            continue
        for item in feed.get("items", []):
            if not item.get("link"):
                continue
            try:
                rows.append(
                    observation_from_payload(
                        entity_type="news",
                        entity_id=str(item["link"]),
                        kind="news_fact_candidate",
                        payload={key: item.get(key) for key in ("title", "summary", "link", "published_at")},
                        source_name=str(feed.get("name", "Public RSS")),
                        source_url=feed_source_url,
                        source_tier="reliable_media",
                        observed_at=_timestamp(feed.get("retrieved_at"), fallback),
                        published_at=item.get("published_at"),
                        confidence=0.45,
                        enters_model=False,
                        model_exclusion_reason=DISPLAY_ONLY_REASONS["news_fact_candidate"],
                        raw_hash=_raw_hash(feed),
                    )
                )
            except ValueError:
                # A malformed or future-dated publication is retained by the
                # raw feed archive, but never enters the model ledger.
                continue
    for weather in snapshot.get("weather", {}).get("weather", []):
        source = weather.get("source", {})
        if not source.get("url") or not source.get("raw_sha256"):
            continue
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(weather["fixture_id"]),
                kind="weather",
                payload={key: weather.get(key) for key in ("kickoff_at", "temperature_c", "humidity_percent", "precipitation_probability_percent", "precipitation_mm", "wind_speed_kmh")},
                source_name="Open-Meteo",
                source_url=source["url"],
                source_tier="authorized",
                observed_at=_timestamp(source.get("retrieved_at"), fallback),
                confidence=0.8,
                **_model_entry_fields(source, True),
                raw_hash=_raw_hash(source),
            )
        )
    for report in snapshot.get("espn_injuries", {}).get("reports", []):
        if not isinstance(report, dict):
            continue
        source = report.get("source")
        competition_id = report.get("competition_id")
        provider_team_id = report.get("provider_team_id")
        if (
            not isinstance(source, dict)
            or not source.get("url")
            or not _raw_hash(source)
            or not competition_id
            or not provider_team_id
        ):
            continue
        try:
            rows.append(
                observation_from_payload(
                    entity_type="team",
                    entity_id=f"espn-team:{competition_id}:{provider_team_id}",
                    kind="player_availability_report",
                    payload={
                        "competition_id": competition_id,
                        "provider_team_id": str(provider_team_id),
                        "team_name": report.get("team_name"),
                        "report_status": report.get("report_status"),
                        "reported_player_count": report.get("reported_player_count"),
                        "scheduled_fixture_ids": report.get("scheduled_fixture_ids", []),
                        "injuries": report.get("injuries", []),
                        "coverage_semantics": "published_bucket_only",
                        "healthy_team_claim": False,
                    },
                    source_name="ESPN injury report",
                    source_url=source["url"],
                    source_tier="reliable_public_provider",
                    observed_at=_timestamp(report.get("retrieved_at") or source.get("retrieved_at"), fallback),
                    confidence=0.7,
                    enters_model=False,
                    model_exclusion_reason=(
                        "league_report_not_fixture_complete_and_provider_terms_require_review"
                    ),
                    raw_hash=_raw_hash(source),
                )
            )
        except (TypeError, ValueError):
            continue
    for event in snapshot.get("sofascore", {}).get("events", []):
        for kind, payload in (("lineup", event.get("lineups")), ("injury", event.get("injuries"))):
            source = event.get("lineups_source") or event.get("source") or {}
            if payload is None or not source.get("url") or not source.get("raw_sha256"):
                continue
            rows.append(
                observation_from_payload(
                    entity_type="fixture",
                    entity_id=str(event["fixture_id"]),
                    kind=kind,
                    payload=payload,
                    source_name="SofaScore",
                    source_url=source["url"],
                    source_tier="authorized",
                    observed_at=_timestamp(source.get("retrieved_at"), fallback),
                    confidence=0.5,
                    enters_model=False,
                    model_exclusion_reason=DISPLAY_ONLY_REASONS[kind],
                    raw_hash=_raw_hash(source),
                )
            )
    fotmob_snapshot = snapshot.get("fotmob", {})
    # Keep any previously archived raw material auditable, but do not append
    # new intelligence from a source whose API path is disallowed by robots.
    for row in fotmob_snapshot.get("lineups", []) if isinstance(fotmob_snapshot, dict) and fotmob_snapshot.get("robots_allowed") is True else []:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        lineups = row.get("lineups")
        if (
            not isinstance(source, dict)
            or not isinstance(lineups, dict)
            or not source.get("url")
            or not source.get("raw_sha256")
        ):
            continue
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=str(row["fixture_id"]),
                kind="fotmob_lineup",
                payload={
                    "event_id": row.get("event_id"),
                    "lineups": lineups,
                    "injuries": row.get("injuries"),
                    "join": row.get("join"),
                },
                source_name="FotMob",
                source_url=source["url"],
                source_tier="reliable_public_provider",
                observed_at=_timestamp(source.get("retrieved_at"), fallback),
                published_at=source.get("effective_at"),
                confidence=0.75 if lineups.get("confirmed") else 0.45,
                **_model_entry_fields(source, bool(lineups.get("model_eligible"))),
                raw_hash=_raw_hash(source),
            )
        )
    for source_key, default_name in (
        ("premier_league_official", "Premier League official"),
        ("laliga_official", "LaLiga official"),
        ("bundesliga_official", "Bundesliga official"),
        ("serie_a_official", "Serie A official"),
        ("ligue1_official", "Ligue 1 official"),
    ):
        official = snapshot.get(source_key, {})
        official_lineups = (
            official.get("lineups", [])
            if isinstance(official, dict) and isinstance(official.get("lineups", []), list)
            else []
        )
        for row in official_lineups:
            if not isinstance(row, dict):
                continue
            source = row.get("source")
            lineups = row.get("lineups")
            if (
                not isinstance(source, dict)
                or not isinstance(lineups, dict)
                or not source.get("url")
                or not source.get("raw_sha256")
            ):
                continue
            prefix = {
                "laliga_official": "laliga",
                "bundesliga_official": "bundesliga",
                "serie_a_official": "seriea",
                "ligue1_official": "ligue1",
            }.get(source_key, "premierleague")
            entity_id = row.get("fixture_id") or f"{prefix}:{row.get('match_id')}"
            if not isinstance(entity_id, str) or entity_id.endswith(":None"):
                continue
            rows.append(
                observation_from_payload(
                    entity_type="fixture",
                    entity_id=entity_id,
                    kind="official_lineup",
                    payload={
                        "match_id": row.get("match_id"),
                        "fixture_id": row.get("fixture_id"),
                        "lineups": lineups,
                        "time_basis": source.get("time_basis"),
                    },
                    source_name=str(source.get("name") or default_name),
                    source_url=source["url"],
                    source_tier="official",
                    observed_at=_timestamp(source.get("retrieved_at"), fallback),
                    published_at=source.get("effective_at"),
                    confidence=0.99 if lineups.get("confirmed") else 0.75,
                    **_model_entry_fields(source, bool(lineups.get("model_eligible"))),
                    raw_hash=_raw_hash(source),
                )
            )
    crawl_snapshot = snapshot.get("crawl4ai", {})
    crawl_pages = (
        crawl_snapshot.get("pages", [])
        if isinstance(crawl_snapshot, dict) and isinstance(crawl_snapshot.get("pages", []), list)
        else []
    )
    for page in crawl_pages:
        if not isinstance(page, dict):
            continue
        source_url = page.get("url")
        raw_hash = _raw_hash(page)
        if not isinstance(source_url, str) or not source_url or not raw_hash:
            continue
        try:
            rows.append(
                observation_from_payload(
                    entity_type="source_page",
                    entity_id=str(page.get("final_url") or source_url),
                    kind="crawl4ai_page",
                    payload={
                        **_crawl4ai_provenance_payload(page),
                        **{
                            key: page.get(key)
                            for key in (
                                "source",
                                "source_id",
                                "fact_source_id",
                                "capture_engine",
                                "capture_role",
                                "source_role",
                                "final_url",
                                "status_code",
                                "content_type",
                                "raw_kind",
                                "content_size",
                                "markdown",
                                "markdown_truncated",
                                "extraction",
                                "extracted_records",
                                "fixture_join",
                                "metadata",
                                "robots",
                            )
                        },
                    },
                    # Crawl4AI is only the execution engine. A page without a
                    # declared provider is deliberately quarantined under an
                    # unknown identity instead of being promoted as a source
                    # named after the crawler.
                    source_name=str(page.get("source") or "unidentified_public_page"),
                    source_url=source_url,
                    source_tier=str(page.get("source_tier") or "reliable_public_provider"),
                    observed_at=_timestamp(page.get("observed_at"), fallback),
                    published_at=page.get("effective_at"),
                        confidence=0.35,
                        enters_model=False,
                        model_exclusion_reason=(
                            "missing_declared_source_identity"
                            if not page.get("source")
                            else "unstructured_browser_capture_requires_source_parser"
                        ),
                        raw_hash=raw_hash,
                )
            )
        except (TypeError, ValueError):
            # A malformed page record remains in the raw Crawl4AI archive but
            # cannot enter the canonical intelligence ledger.
            continue
        # A source-specific parser may have produced an exact canonical
        # fixture join. Keep market and fixture-context facts as separate
        # immutable observations so Sites can compare providers without
        # mistaking the browser page itself for a model feature. The current
        # Crawl4AI operator config has unknown licence/model-use status, so
        # every joined record remains comparison-only.
        joined_records = page.get("joined_fixture_records")
        if not isinstance(joined_records, list):
            joined_records = []
        for joined in joined_records:
            if not isinstance(joined, dict):
                continue
            fixture_id = joined.get("fixture_id")
            if not isinstance(fixture_id, str) or not fixture_id:
                continue
            has_market = isinstance(joined.get("one_x_two_odds"), dict)
            observation_kind = (
                "crawl4ai_fixture_market"
                if has_market
                else "crawl4ai_fixture_context"
            )
            exclusion_reason = (
                "public_source_market_comparison_requires_license_review"
                if has_market
                else "public_page_fixture_context_requires_source_and_time_review"
            )
            try:
                rows.append(
                    observation_from_payload(
                        entity_type="fixture",
                        entity_id=fixture_id,
                        kind=observation_kind,
                        payload={
                            **_crawl4ai_provenance_payload(page),
                            **{
                                key: joined.get(key)
                                for key in (
                                    "source_parser",
                                    "fact_type",
                                    "source_match_id",
                                    "source_event_id",
                                    "match_url",
                                    "competition_id",
                                    "home_team",
                                    "away_team",
                                    "canonical_home_team",
                                    "canonical_away_team",
                                    "source_kickoff_at",
                                    "canonical_kickoff_at",
                                    "kickoff_delta_seconds",
                                    "source_timezone",
                                    "join_status",
                                    "join_confidence",
                                    "pre_match_eligible",
                                    "status",
                                    "one_x_two_odds",
                                    # Keep the structured Crawl4AI admission
                                    # decision beside the joined fact.  This is
                                    # audit metadata only: the observation is
                                    # still explicitly display-only and the
                                    # admission object is never interpreted as a
                                    # model feature.
                                    "admission",
                                )
                            },
                            # Carry the page lineage into the immutable joined
                            # fact. Crawl4AI is only the shared execution
                            # layer; these fields identify the declared
                            # provider, parser and entry/detail chain for D1
                            # and offline readers alike.
                            "source_id": page.get("source_id"),
                            "fact_source_id": page.get("fact_source_id"),
                            "capture_engine": page.get("capture_engine") or "Crawl4AI",
                            "capture_role": page.get("capture_role"),
                            "source_role": page.get("source_role"),
                            "parser_contract": page.get("parser_contract") or (
                                page.get("extraction", {}).get("parser_contract")
                                if isinstance(page.get("extraction"), dict)
                                else None
                            ),
                            "crawl_stage": page.get("crawl_stage") or "index",
                            "parent_url": page.get("parent_url"),
                            "parent_content_sha256": page.get("parent_content_sha256"),
                            "follow_reason": page.get("follow_reason"),
                            "page_url": page.get("url"),
                            "page_final_url": page.get("final_url"),
                            "page_content_sha256": page.get("content_sha256") or raw_hash,
                        },
                        source_name=str(page.get("source") or "unidentified_public_page"),
                        source_url=source_url,
                        source_tier=str(page.get("source_tier") or "reliable_public_provider"),
                        observed_at=_timestamp(page.get("observed_at"), fallback),
                        published_at=page.get("effective_at"),
                        confidence=float(joined.get("join_confidence") or 0.0),
                        enters_model=False,
                        model_exclusion_reason=exclusion_reason,
                        raw_hash=raw_hash,
                    )
                )
            except (TypeError, ValueError):
                # A malformed join is isolated to this source record; the raw
                # page and other valid observations remain auditable.
                continue
    # Keep one immutable row per player evidence item in addition to the
    # fixture-level roster/lineup record.  Provider ids stay in the payload;
    # canonical foreign keys remain null until a trusted registrar binds them.
    rows.extend(snapshot_player_observations(snapshot, fallback=fallback))
    # A feed can repeat the same item under multiple query buckets.  Keep one
    # immutable record per exact observation while preserving distinct
    # entities, timestamps, and source responses.
    deduplicated: list[Observation] = []
    seen: set[str] = set()
    for row in rows:
        key = ObservationLedger._record_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(row)
    return deduplicated


def archive_snapshot_intelligence(snapshot: dict, ledger_path: Path) -> dict[str, int]:
    return ObservationLedger(ledger_path).append(snapshot_observations(snapshot))


__all__ = ["archive_snapshot_intelligence", "snapshot_observations"]
