"""Pure, fail-closed source-rights policy for the v260 source inventory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Final


POLICY_VERSION: Final = "v260"


class UseCase(str, Enum):
    """Closed set of operations governed by the rights policy."""

    NETWORK_FETCH = "network_fetch"
    SERVE_CURRENT = "serve_current"
    MODEL_INPUT = "model_input"
    TRAINING = "training"
    REDISTRIBUTION = "redistribution"


class SourceId(str, Enum):
    """Stable source identities covered by policy v260."""

    OPENFOOTBALL_CURRENT = "openfootball_current"
    OPENFOOTBALL_HISTORICAL = "openfootball_historical"
    OPENLIGADB_SECONDARY_RESULTS = "openligadb_secondary_results"
    CFL_OFFICIAL_CURRENT = "cfl_official_current"
    ESPN_SCHEDULE_SUMMARY = "espn_schedule_summary"
    ESPN_MARKET_SUMMARY = "espn_market_summary"
    ESPN_TEAM_ROSTERS = "espn_team_rosters"
    ESPN_INJURY_REPORTS = "espn_injury_reports"
    UNDERSTAT_XG = "understat_xg"
    OFFICIAL_LEAGUE_LINEUPS = "official_league_lineups"
    OFFICIAL_PREMIER_LEAGUE_LINEUPS = "official_premier_league_lineups"
    OFFICIAL_LALIGA_LINEUPS = "official_laliga_lineups"
    OFFICIAL_BUNDESLIGA_LINEUPS = "official_bundesliga_lineups"
    OFFICIAL_SERIE_A_LINEUPS = "official_serie_a_lineups"
    OFFICIAL_LIGUE1_LINEUPS = "official_ligue1_lineups"
    PUBLIC_RSS_NEWS = "public_rss_news"
    OPEN_METEO_GEOCODING = "open_meteo_geocoding"
    OPEN_METEO_WEATHER = "open_meteo_weather"
    SOFASCORE_PREMATCH = "sofascore_prematch"
    SPORTS_LOTTERY_OFFICIAL = "sports_lottery_official"
    ODDSTORM_MARKET_COMPARISON = "oddstorm_market_comparison"
    ODDSTORM_MARKET_HISTORY = "oddstorm_market_history"
    FOOTBALL_DATA_HISTORICAL = "football_data_historical"
    FBREF_PUBLIC_STATS = "fbref_public_stats"
    WHOSCORED_PUBLIC_PAGES = "whoscored_public_pages"
    PREMIER_LEAGUE_PUBLIC_PAGES = "premier_league_public_pages"
    LALIGA_PUBLIC_PAGES = "laliga_public_pages"
    LALIGA_OFFICIAL_NEWS = "laliga_official_news"
    BUNDESLIGA_PUBLIC_PAGES = "bundesliga_public_pages"
    SERIEA_PUBLIC_PAGES = "seriea_public_pages"
    LIGUE1_OFFICIAL_NEWS = "ligue1_official_news"
    CLUBELO_PUBLIC_RATINGS = "clubelo_public_ratings"
    SOFIFA_PUBLIC_REFERENCE = "sofifa_public_reference"
    FOTMOB_PUBLIC_API = "fotmob_public_api"
    WIKIDATA_ENTITIES = "wikidata_entities"
    MET_NORWAY_WEATHER = "met_norway_weather"
    OSM_GEODATA = "osm_geodata"
    PAPPALARDO_WYSCOUT_HISTORICAL = "pappalardo_wyscout_historical"
    FIVE_HUNDRED_LEAGUE_PUBLIC_PAGES = "five_hundred_league_public_pages"
    LAZQ_PUBLIC_MIRROR = "lazq_public_mirror"
    FOOTBALL_DATA_CHINA_PUBLIC_CSV = "football_data_china_public_csv"
    SEVENM_CSL_PUBLIC_FIXTURE_SCRIPT = "sevenm_csl_public_fixture_script"
    CHECKBESTODDS_PUBLIC_PAGES = "checkbestodds_public_pages"


class DecisionValue(str, Enum):
    """Possible fail-closed policy outcomes."""

    ALLOW = "allow"
    BLOCK = "block"
    FUTURE_DISABLED = "future_disabled"


@dataclass(frozen=True, slots=True)
class _RightsMetadata:
    license_url: str | None = None
    terms_url: str | None = None
    attribution_required: bool = False
    share_alike_required: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """Typed, immutable and canonically serializable policy result."""

    policy_version: str
    source_id: str
    use_case: str
    decision: DecisionValue
    rights_status: str
    license_url: str | None
    terms_url: str | None
    commercial_reuse_verified: bool
    access_allowed: bool
    model_eligible: bool
    network_opened: bool
    attribution_required: bool
    share_alike_required: bool
    reason: str
    ignored_inputs: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        """Return whether this exact source/use-case pair is allowed."""

        return self.access_allowed and self.decision is DecisionValue.ALLOW

    @property
    def is_allowed(self) -> bool:
        """Alias used by enforcement call sites."""

        return self.allowed

    def as_dict(self) -> dict[str, object]:
        """Return JSON-only primitives with a deterministic key order."""

        payload: dict[str, object] = {
            "policy_version": self.policy_version,
            "source_id": self.source_id,
            "use_case": self.use_case,
            "decision": self.decision.value,
            "rights_status": self.rights_status,
            "license_url": self.license_url,
            "terms_url": self.terms_url,
            "commercial_reuse_verified": self.commercial_reuse_verified,
            "access_allowed": self.access_allowed,
            "model_eligible": self.model_eligible,
            "network_opened": self.network_opened,
            "attribution_required": self.attribution_required,
            "share_alike_required": self.share_alike_required,
            "reason": self.reason,
            "ignored_inputs": list(self.ignored_inputs),
        }
        return {key: payload[key] for key in sorted(payload)}


class SourceRightsBlocked(PermissionError):
    """Raised when an enforcement call requests a non-allowed decision."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__(
            f"source rights blocked by {decision.policy_version}: "
            f"source_id={decision.source_id} use_case={decision.use_case} "
            f"decision={decision.decision.value}"
        )


_ALL_SOURCE_IDS = frozenset(source_id.value for source_id in SourceId)
_ALL_USE_CASES = frozenset(use_case.value for use_case in UseCase)
_EMPTY_ENVELOPE_FIELDS: Final = frozenset(
    {
        "events",
        "competition_reports",
        "date_only_fixtures",
        "fallbacks",
        "fixture_updates",
        "fixtures",
        "geocodes",
        "history",
        "incidents",
        "injuries",
        "lineups",
        "lines",
        "markets",
        "match_stats",
        "matches",
        "news",
        "observations",
        "odds",
        "odds_history",
        "pages",
        "records",
        "reports",
        "results",
        "rosters",
        "rows",
        "team_status",
        "recent_results",
        "upcoming_3_days",
        "upcoming_7_days",
        "venues",
        "weather",
    }
)
_OPENLIGADB_ALLOWED_USE_CASES = frozenset(
    {UseCase.NETWORK_FETCH.value, UseCase.SERVE_CURRENT.value}
)
_MET_NORWAY_ALLOWED_USE_CASES = frozenset(
    {
        UseCase.NETWORK_FETCH.value,
        UseCase.SERVE_CURRENT.value,
        UseCase.MODEL_INPUT.value,
        UseCase.REDISTRIBUTION.value,
    }
)
_WIKIDATA_ALLOWED_USE_CASES = frozenset(
    {
        UseCase.NETWORK_FETCH.value,
        UseCase.SERVE_CURRENT.value,
        UseCase.MODEL_INPUT.value,
        UseCase.REDISTRIBUTION.value,
    }
)
_WRITTEN_PERMISSION_TERMS: Final = {
    SourceId.ESPN_SCHEDULE_SUMMARY.value: "https://disneytermsofuse.com/english/",
    SourceId.ESPN_MARKET_SUMMARY.value: "https://disneytermsofuse.com/english/",
    SourceId.ESPN_TEAM_ROSTERS.value: "https://disneytermsofuse.com/english/",
    SourceId.ESPN_INJURY_REPORTS.value: "https://disneytermsofuse.com/english/",
    SourceId.ODDSTORM_MARKET_COMPARISON.value: "https://www.oddstorm.com/terms",
    SourceId.ODDSTORM_MARKET_HISTORY.value: "https://www.oddstorm.com/terms",
}
_FUTURE_SOURCE_METADATA: Final = {
    SourceId.WIKIDATA_ENTITIES.value: _RightsMetadata(
        license_url="https://www.wikidata.org/wiki/Wikidata:Licensing",
    ),
    SourceId.MET_NORWAY_WEATHER.value: _RightsMetadata(
        license_url="https://creativecommons.org/licenses/by/4.0/",
        terms_url="https://api.met.no/doc/TermsOfService",
        attribution_required=True,
    ),
    SourceId.OSM_GEODATA.value: _RightsMetadata(
        license_url="https://opendatacommons.org/licenses/odbl/1-0/",
        terms_url="https://www.openstreetmap.org/copyright",
        attribution_required=True,
        share_alike_required=True,
    ),
    SourceId.PAPPALARDO_WYSCOUT_HISTORICAL.value: _RightsMetadata(
        license_url="https://creativecommons.org/licenses/by/4.0/",
        terms_url="https://figshare.com/collections/Soccer_match_event_dataset/4415000",
        attribution_required=True,
    ),
}


def _decision(
    *,
    source_id: str,
    use_case: str,
    decision: DecisionValue,
    rights_status: str,
    reason: str,
    metadata: _RightsMetadata = _RightsMetadata(),
    commercial_reuse_verified: bool = False,
    model_eligible: bool = False,
    ignored_inputs: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        policy_version=POLICY_VERSION,
        source_id=source_id,
        use_case=use_case,
        decision=decision,
        rights_status=rights_status,
        license_url=metadata.license_url,
        terms_url=metadata.terms_url,
        commercial_reuse_verified=commercial_reuse_verified,
        access_allowed=decision is DecisionValue.ALLOW,
        model_eligible=model_eligible,
        network_opened=False,
        attribution_required=metadata.attribution_required,
        share_alike_required=metadata.share_alike_required,
        reason=reason,
        ignored_inputs=ignored_inputs,
    )


def _ignored_external_inputs(
    *,
    authorization_reference: object | None,
    operator_registry: object | None,
    config: object | None,
) -> tuple[str, ...]:
    supplied = {
        "authorization_reference": authorization_reference,
        "operator_registry": operator_registry,
        "config": config,
    }
    return tuple(sorted(name for name, value in supplied.items() if value is not None))


def decide_source_rights(
    source_id: SourceId | str,
    use_case: UseCase | str,
    *,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> Decision:
    """Evaluate one source/use-case pair without opening the network."""

    source_value = source_id.value if isinstance(source_id, SourceId) else source_id
    use_case_value = use_case.value if isinstance(use_case, UseCase) else use_case
    ignored_inputs = _ignored_external_inputs(
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )
    if use_case_value not in _ALL_USE_CASES:
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.BLOCK,
            rights_status="unknown_use_case_fail_closed",
            reason="The use case is not part of the closed v260 policy contract.",
            ignored_inputs=ignored_inputs,
        )
    if source_value in {
        SourceId.OPENFOOTBALL_CURRENT.value,
        SourceId.OPENFOOTBALL_HISTORICAL.value,
    }:
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.ALLOW,
            rights_status="verified_cc0",
            metadata=_RightsMetadata(
                license_url=(
                    "https://github.com/openfootball/football.json/blob/master/LICENSE.md"
                )
            ),
            commercial_reuse_verified=True,
            model_eligible=True,
            reason="OpenFootball CC0 is verified for every v260 use case.",
            ignored_inputs=ignored_inputs,
        )
    if source_value == SourceId.OPENLIGADB_SECONDARY_RESULTS.value:
        allowed = use_case_value in _OPENLIGADB_ALLOWED_USE_CASES
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.ALLOW if allowed else DecisionValue.BLOCK,
            rights_status="verified_odbl_isolated",
            metadata=_RightsMetadata(
                license_url="https://www.openligadb.de/lizenz",
                attribution_required=True,
                share_alike_required=True,
            ),
            commercial_reuse_verified=True,
            reason=(
                "OpenLigaDB ODbL access is limited to the isolated fetch/current-serving lane."
                if allowed
                else (
                    "OpenLigaDB ODbL model, training, and redistribution contracts are not "
                    "implemented in v260."
                )
            ),
            ignored_inputs=ignored_inputs,
        )
    if source_value == SourceId.MET_NORWAY_WEATHER.value:
        allowed = use_case_value in _MET_NORWAY_ALLOWED_USE_CASES
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.ALLOW if allowed else DecisionValue.BLOCK,
            rights_status="verified_cc_by_forecast_current_only",
            metadata=_FUTURE_SOURCE_METADATA[source_value],
            commercial_reuse_verified=allowed,
            model_eligible=allowed,
            reason=(
                "MET Norway CC BY weather forecasts are allowed for current/model use and redistribution with attribution."
                if allowed
                else "MET Norway current forecasts are not historical training observations."
            ),
            ignored_inputs=ignored_inputs,
        )
    if source_value == SourceId.WIKIDATA_ENTITIES.value:
        allowed = use_case_value in _WIKIDATA_ALLOWED_USE_CASES
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.ALLOW if allowed else DecisionValue.BLOCK,
            rights_status="verified_cc0_structured_data",
            metadata=_FUTURE_SOURCE_METADATA[source_value],
            commercial_reuse_verified=allowed,
            model_eligible=allowed,
            reason=(
                "Wikidata structured data CC0 is allowed for bounded entity/venue coordinates."
                if allowed
                else "Wikidata is not admitted for historical training in this lane."
            ),
            ignored_inputs=ignored_inputs,
        )
    if source_value in _FUTURE_SOURCE_METADATA:
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.FUTURE_DISABLED,
            rights_status="future_disabled_pending_adapter_attribution_contract",
            metadata=_FUTURE_SOURCE_METADATA[source_value],
            reason=(
                "The source remains disabled until its adapter and attribution contract are "
                "implemented and verified."
            ),
            ignored_inputs=ignored_inputs,
        )
    if source_value not in _ALL_SOURCE_IDS:
        return _decision(
            source_id=source_value,
            use_case=use_case_value,
            decision=DecisionValue.BLOCK,
            rights_status="unknown_source_fail_closed",
            reason="The source is not part of the closed v260 source inventory.",
            ignored_inputs=ignored_inputs,
        )
    terms_url = _WRITTEN_PERMISSION_TERMS.get(source_value)
    return _decision(
        source_id=source_value,
        use_case=use_case_value,
        decision=DecisionValue.BLOCK,
        rights_status=(
            "blocked_pending_express_written_permission"
            if terms_url is not None
            else "blocked_rights_not_verified"
        ),
        metadata=_RightsMetadata(terms_url=terms_url),
        reason=(
            "Provider rights require a verified policy release; external authorization strings "
            "cannot grant access."
            if terms_url is not None
            else "No verified v260 rights contract authorizes this source/use-case pair."
        ),
        ignored_inputs=ignored_inputs,
    )


def require_source_rights(
    source_id: SourceId | str,
    use_case: UseCase | str,
    *,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> Decision:
    """Return an allowed decision or raise the fixed typed policy exception."""

    result = decide_source_rights(
        source_id,
        use_case,
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )
    if not result.is_allowed:
        raise SourceRightsBlocked(result)
    return result


def rights_blocked_envelope(
    source_id: SourceId | str,
    *,
    provider: str,
    checked_at: str,
    empty_fields: tuple[str, ...],
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, object]:
    """Build one canonical pre-network policy block with empty fact collections."""

    provider_value = provider.strip() if isinstance(provider, str) else ""
    if not provider_value or len(provider_value) > 160:
        raise ValueError("provider must be a non-empty bounded string")
    try:
        parsed_checked_at = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("checked_at must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed_checked_at.tzinfo is None or parsed_checked_at.utcoffset() is None:
        raise ValueError("checked_at must be a timezone-aware ISO-8601 timestamp")
    invalid_fields = sorted(set(empty_fields) - _EMPTY_ENVELOPE_FIELDS)
    if invalid_fields:
        raise ValueError(f"unsupported empty field: {invalid_fields[0]}")
    decision = decide_source_rights(
        source_id,
        UseCase.NETWORK_FETCH,
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )
    if decision.is_allowed:
        raise ValueError("rights_blocked_envelope cannot represent an allowed decision")
    payload: dict[str, object] = {
        "schema_version": "matchline.source_rights_result.v1",
        "provider": provider_value,
        "status": "rights_blocked",
        "checked_at": parsed_checked_at.astimezone(timezone.utc).isoformat(),
        "retrieved_at": None,
        "network_opened": False,
        "rights": decision.as_dict(),
        "rights_status": decision.rights_status,
        "access_allowed": False,
        "commercial_reuse_verified": decision.commercial_reuse_verified,
        "model_eligible": False,
        "errors": [],
    }
    for field in sorted(set(empty_fields)):
        payload[field] = []
    return {key: payload[key] for key in sorted(payload)}


def rights_blocked_fetch_envelope(
    source_id: SourceId | str,
    *,
    provider: str,
    now: object | None,
    empty_fields: tuple[str, ...],
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, object]:
    """Build a pre-validation block even when an untrusted timestamp is malformed."""

    checked_at = (
        now.astimezone(timezone.utc)
        if isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None
        else datetime.now(timezone.utc)
    )
    return rights_blocked_envelope(
        source_id,
        provider=provider,
        checked_at=checked_at.isoformat(),
        empty_fields=empty_fields,
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )


__all__ = [
    "Decision",
    "DecisionValue",
    "POLICY_VERSION",
    "SourceId",
    "SourceRightsBlocked",
    "UseCase",
    "decide_source_rights",
    "rights_blocked_envelope",
    "rights_blocked_fetch_envelope",
    "require_source_rights",
]
