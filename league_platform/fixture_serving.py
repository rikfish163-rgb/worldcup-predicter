"""Fail-closed rights gate for fixture consumption surfaces.

Append-only archives deliberately keep historical provider snapshots intact.
Each caller names its use case explicitly at a write boundary.  A fixture row
cannot grant itself rights with authorization strings or booleans; the stable
source policy remains the sole authority.  The provider-neutral
``fixture_feed`` lane is the current schema and is not relabelled or inferred
from the legacy section.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from league_platform.fixture_feed import (
    LEGACY_FIXTURE_FEED_KEY,
    fixture_feed_key,
    fixture_rows,
    fixture_feed_section,
)
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.live_sources.openligadb import (
    OPENLIGADB_HOST,
    OPENLIGADB_MATCH_PATH,
    OPENLIGADB_PATH,
)
from league_platform.source_rights import (
    SourceId,
    UseCase,
    decide_source_rights,
)


ESPN_AUTHORIZED_RIGHTS_STATUS = "operator_authorization_reference_supplied"
LEGACY_ESPN_BLOCK_REASON = "legacy_espn_serving_rights_missing"
_PROVENANCE_PROVIDER_SOURCE_IDS = {
    "OpenFootball": SourceId.OPENFOOTBALL_CURRENT,
    "OpenLigaDB": SourceId.OPENLIGADB_SECONDARY_RESULTS,
    "CFL official": SourceId.CFL_OFFICIAL_CURRENT,
    "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY,
    "Premier League official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS,
    "LaLiga official": SourceId.OFFICIAL_LALIGA_LINEUPS,
    "Bundesliga official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
    "Serie A official": SourceId.OFFICIAL_SERIE_A_LINEUPS,
    "Ligue 1 official": SourceId.OFFICIAL_LIGUE1_LINEUPS,
    "MET Norway Locationforecast": SourceId.MET_NORWAY_WEATHER,
    "Wikidata structured data": SourceId.WIKIDATA_ENTITIES,
    "Open-Meteo Geocoding": SourceId.OPEN_METEO_GEOCODING,
}


def _nested_fixture_provenance_status(
    fixture: Mapping[str, Any],
    *,
    root_source: Mapping[str, Any],
    use_case: UseCase,
) -> dict[str, Any] | None:
    """Reject a blocked provider hidden behind an allowed canonical row."""

    field_sources = fixture.get("field_sources")
    if field_sources is not None:
        if not isinstance(field_sources, Mapping):
            return {
                "status": "blocked",
                "reason": "fixture_field_source_provenance_malformed",
            }
        for field_name, nested_source in field_sources.items():
            if not isinstance(field_name, str) or not isinstance(
                nested_source,
                Mapping,
            ):
                return {
                    "status": "blocked",
                    "reason": "fixture_field_source_provenance_malformed",
                }
            provider = nested_source.get("name") or nested_source.get("provider")
            if not isinstance(provider, str) or not provider.strip():
                return {
                    "status": "blocked",
                    "reason": "fixture_field_source_provenance_missing",
                }
            if provider == "OpenFootball":
                if any(
                    nested_source.get(key) != root_source.get(key)
                    for key in ("name", "source_id", "url", "license")
                ):
                    return {
                        "status": "blocked",
                        "reason": "openfootball_source_contract_unverified",
                    }
                continue
            source_id = _PROVENANCE_PROVIDER_SOURCE_IDS.get(provider)
            if source_id is None:
                return {
                    "status": "blocked",
                    "reason": f"source_rights_unknown_provider:{provider}",
                }
            decision = decide_source_rights(source_id, use_case)
            if not decision.is_allowed:
                return {
                    "status": "blocked",
                    "reason": (
                        f"source_rights_blocked:{source_id.value}:"
                        f"{use_case.value}"
                    ),
                    "policy": decision.as_dict(),
                }

    overlay = fixture.get("schedule_overlay")
    if overlay not in (None, {}, []):
        if not isinstance(overlay, Mapping):
            return {
                "status": "blocked",
                "reason": "fixture_schedule_overlay_provenance_malformed",
            }
        provider = overlay.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            return {
                "status": "blocked",
                "reason": "fixture_schedule_overlay_provenance_missing",
            }
        source_id = _PROVENANCE_PROVIDER_SOURCE_IDS.get(provider)
        if source_id is None:
            return {
                "status": "blocked",
                "reason": f"source_rights_unknown_provider:{provider}",
            }
        decision = decide_source_rights(source_id, use_case)
        if not decision.is_allowed:
            return {
                "status": "blocked",
                "reason": (
                    f"source_rights_blocked:{source_id.value}:"
                    f"{use_case.value}"
                ),
                "policy": decision.as_dict(),
            }
    return None


def fixture_commercial_reuse_status(
    fixture: Mapping[str, Any],
    *,
    legacy_espn_authorized: bool = False,
    use_case: UseCase = UseCase.SERVE_CURRENT,
) -> dict[str, Any]:
    """Return the row-level commercial publication decision.

    OpenFootball's exact, code-registered CC0 source contract is the sole
    currently enabled all-use-case source.  Payload-carried authorization
    metadata is audit evidence only and cannot elevate a blocked policy row.
    """

    source = fixture.get("source")
    if not isinstance(source, Mapping):
        return {
            "status": "blocked",
            "reason": "fixture_source_provenance_missing",
            "provider": None,
        }
    provider = source.get("name")
    if not isinstance(provider, str) or not provider.strip():
        return {
            "status": "blocked",
            "reason": "fixture_source_provenance_missing",
            "provider": None,
        }
    nested_status = _nested_fixture_provenance_status(
        fixture,
        root_source=source,
        use_case=use_case,
    )
    if nested_status is not None:
        return nested_status
    if provider == "OpenFootball":
        policy = decide_source_rights(SourceId.OPENFOOTBALL_CURRENT, use_case)
        source_id = source.get("source_id")
        contract = (
            OPENFOOTBALL_SOURCES.get(source_id)
            if isinstance(source_id, str)
            else None
        )
        if (
            source.get("license") != "CC0-1.0"
            or not policy.is_allowed
            or contract is None
            or source.get("url") != contract.get("url")
            or fixture.get("competition_id") != contract.get("competition_id")
            or fixture.get("season") != contract.get("season")
        ):
            return {
                "status": "blocked",
                "reason": "openfootball_source_contract_unverified",
                "provider": provider,
            }
        return {
            "status": "ok",
            "reason": "openfootball_cc0_allowlisted_source_verified",
            "provider": provider,
            "source_id": source_id,
            "raw_hash_role": "declared_lineage_digest_not_recomputed_by_publisher",
            "policy": policy.as_dict(),
        }
    if provider == "OpenLigaDB":
        policy = decide_source_rights(SourceId.OPENLIGADB_SECONDARY_RESULTS, use_case)
        parsed_url = urlparse(str(source.get("url") or ""))
        try:
            parsed_port = parsed_url.port
        except ValueError:
            parsed_port = -1
        url_allowed = (
            parsed_url.scheme == "https"
            and parsed_url.hostname == OPENLIGADB_HOST
            and parsed_port in (None, 443)
            and parsed_url.username is None
            and parsed_url.password is None
            and not parsed_url.query
            and not parsed_url.fragment
            and bool(
                OPENLIGADB_PATH.fullmatch(parsed_url.path)
                or OPENLIGADB_MATCH_PATH.fullmatch(parsed_url.path)
            )
        )
        if (
            policy.is_allowed
            and url_allowed
            and source.get("license") == "ODbL-1.0"
            and source.get("license_url") == policy.license_url
            and source.get("attribution_required") is True
        ):
            return {
                "status": "ok",
                "reason": "openligadb_odbl_current_display_contract_verified",
                "provider": provider,
                "policy": policy.as_dict(),
            }
        return {
            "status": "blocked",
            "reason": f"source_rights_blocked:{SourceId.OPENLIGADB_SECONDARY_RESULTS.value}:{use_case.value}",
            "provider": provider,
            "policy": policy.as_dict(),
        }
    # Rights metadata carried by the same untrusted fixture row cannot grant
    # commercial publication.  ESPN legacy authorization strings and booleans
    # remain preserved for audit, but v260 intentionally ignores them.
    policy_source_id = {
        "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY,
        "CFL official": SourceId.CFL_OFFICIAL_CURRENT,
    }.get(provider)
    if policy_source_id is not None:
        policy = decide_source_rights(policy_source_id, use_case)
        return {
            "status": "blocked",
            "reason": f"source_rights_blocked:{policy_source_id.value}:{use_case.value}",
            "provider": provider,
            "policy": policy.as_dict(),
        }
    return {
        "status": "blocked",
        "reason": f"source_rights_unknown_provider:{provider}",
        "provider": provider,
    }


def fixture_serving_status(
    snapshot: Mapping[str, Any],
    *,
    use_case: UseCase = UseCase.SERVE_CURRENT,
) -> dict[str, Any]:
    """Return a bounded use-case decision without mutating the snapshot."""

    selected_key = fixture_feed_key(snapshot)
    if selected_key is None:
        return {
            "status": "blocked",
            "reason": "canonical_fixture_feed_missing",
            "fixture_feed_key": None,
            "provider": None,
        }
    if selected_key != LEGACY_FIXTURE_FEED_KEY:
        section = fixture_feed_section(snapshot)
        raw_fixtures = section.get("fixtures")
        if (
            not isinstance(section.get("provider"), str)
            or not str(section.get("provider")).strip()
            or not isinstance(raw_fixtures, list)
            or any(not isinstance(row, dict) for row in raw_fixtures)
        ):
            return {
                "status": "blocked",
                "reason": "canonical_fixture_feed_malformed",
                "fixture_feed_key": selected_key,
                "provider": section.get("provider"),
            }
        if not raw_fixtures:
            return {
                "status": "blocked",
                "reason": "canonical_fixture_feed_empty",
                "fixture_feed_key": selected_key,
                "provider": section.get("provider"),
            }
        for index, fixture in enumerate(fixture_rows(snapshot)):
            rights = fixture_commercial_reuse_status(fixture, use_case=use_case)
            if rights["status"] != "ok":
                return {
                    "status": "blocked",
                    "reason": rights["reason"],
                    "fixture_feed_key": selected_key,
                    "provider": rights.get("provider") or section.get("provider"),
                    "fixture_index": index,
                }
        return {
            "status": "ok",
            "reason": "current_fixture_feed_selected",
            "fixture_feed_key": selected_key,
            "provider": section.get("provider"),
        }

    section = fixture_feed_section(snapshot)
    authorization_reference = section.get("authorization_reference")
    authorized = (
        section.get("provider") == "ESPN"
        and section.get("rights_status") == ESPN_AUTHORIZED_RIGHTS_STATUS
        and section.get("access_allowed") is True
        and isinstance(authorization_reference, str)
        and bool(authorization_reference.strip())
        and section.get("commercial_reuse_verified_by_code") is True
    )
    if authorized:
        policy = decide_source_rights(
            SourceId.ESPN_SCHEDULE_SUMMARY,
            use_case,
        )
        return {
            "status": "blocked",
            "reason": (
                "source_rights_blocked:"
                f"{SourceId.ESPN_SCHEDULE_SUMMARY.value}:{use_case.value}"
            ),
            "fixture_feed_key": selected_key,
            "provider": "ESPN",
            "rights_status": section.get("rights_status"),
            "policy": policy.as_dict(),
        }
    return {
        "status": "blocked",
        "reason": LEGACY_ESPN_BLOCK_REASON,
        "fixture_feed_key": selected_key,
        "provider": section.get("provider"),
        "rights_status": section.get("rights_status"),
        "access_allowed": section.get("access_allowed") is True,
        "authorization_reference_present": (
            isinstance(authorization_reference, str)
            and bool(authorization_reference.strip())
        ),
        "commercial_reuse_verified_by_code": (
            section.get("commercial_reuse_verified_by_code") is True
        ),
    }


def require_fixture_serving(
    snapshot: Mapping[str, Any],
    *,
    use_case: UseCase = UseCase.SERVE_CURRENT,
) -> dict[str, Any]:
    """Raise before an artifact consumes a fixture feed for ``use_case``."""

    decision = fixture_serving_status(snapshot, use_case=use_case)
    if decision["status"] != "ok":
        raise ValueError(str(decision["reason"]))
    return decision


__all__ = [
    "ESPN_AUTHORIZED_RIGHTS_STATUS",
    "LEGACY_ESPN_BLOCK_REASON",
    "fixture_commercial_reuse_status",
    "fixture_serving_status",
    "require_fixture_serving",
]
