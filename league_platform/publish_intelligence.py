"""Publish the local append-only intelligence ledger to the protected Sites API.

The command is intentionally opt-in: both the HTTPS endpoint and bearer token
must be supplied through environment variables.  It streams the JSONL ledger,
uses an append-only receipt journal to resume completed batches, and retries
only failures that are plausibly transient.  It never prints the token or
persists it to the repository.
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import hmac
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from league_platform.fixture_feed import fixture_rows
from league_platform.intelligence import Observation, ObservationLedger, utc
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_ALLOWED_URLS
from league_platform.news_evidence import resolve_news_rows
from league_platform.source_rights import SourceId, UseCase, decide_source_rights

try:  # pragma: no cover - the deployed runtime is Linux, but keep imports portable.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


MAX_OBSERVATIONS_PER_REQUEST = 200
MAX_SOURCE_RUN_METADATA_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_WIRE_PAYLOAD_DEPTH = 8
MAX_WIRE_PAYLOAD_KEYS = 64
MAX_WIRE_PAYLOAD_ARRAY_ITEMS = 100
MAX_WIRE_PAYLOAD_STRING_LENGTH = 20_000
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 30.0
TRANSIENT_HTTP_STATUS = {408, 425, 429}
SITES_BYPASS_TOKEN_ENV = "MATCHLINE_SITES_BYPASS_TOKEN"
PUBLISH_USER_AGENT_ENV = "MATCHLINE_PUBLISH_USER_AGENT"
PRODUCER_KEY_ID_ENV = "MATCHLINE_PRODUCER_KEY_ID"
PRODUCER_SIGNING_SECRET_ENV = "MATCHLINE_PRODUCER_SIGNING_SECRET"
DEFAULT_PUBLISH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MatchlinePublisher/1.0"
)
PRODUCER_ATTESTATION_SCHEMA_VERSION = "matchline.producer_attestation.v1"
PRODUCER_NAME = "matchline-python-publisher"
PRODUCER_VERSION = "v260"
OPENFOOTBALL_RAW_PROVENANCE_CANDIDATE = (
    "candidate_only_unverified_raw_provenance"
)
PRODUCER_ATTESTATION_TTL_SECONDS = 300
PUBLICATION_RECEIPT_SCHEMA_VERSION = 3
LEDGER_CURSOR_SCHEMA_VERSION = 3
LEDGER_CURSOR_PREFIX_BYTES = 4096


@dataclass(frozen=True)
class PublicationMaturityContext:
    """Filesystem identities revalidated immediately before a remote write."""

    receipt_path: Path
    artifact_paths: Mapping[str, Path]
    runtime_root: Path
    repository_root: Path


@dataclass(frozen=True)
class ProducerAttestationContext:
    """Server-verifiable body and source-policy binding for one write lane."""

    key_id: str
    signing_secret: bytes = field(repr=False)
    stream: str
    rights_use_case: str
    source_policy_ids: tuple[str, ...]
    publication_epoch: str
    maturity_receipt_raw_sha256: str
    live_snapshot_raw_sha256: str


def require_raw_source_producer_receipt(
    source_policy_ids: set[str] | tuple[str, ...] | list[str],
    *,
    dry_run: bool,
) -> str:
    """Block OpenFootball facts until independent raw admission is verified."""

    normalized = {str(value).strip() for value in source_policy_ids}
    if SourceId.OPENFOOTBALL_CURRENT.value not in normalized:
        return "not_applicable"
    if not dry_run:
        raise ValueError("raw_producer_receipt_required")
    return OPENFOOTBALL_RAW_PROVENANCE_CANDIDATE


def require_openfootball_ledger_raw_admission(
    snapshot_path: Path | None,
    *,
    openfootball_raw_archive_dir: Path | str | None,
    ledger_observation_identities: Iterable[str] | None = None,
) -> str:
    """Re-verify the current snapshot before publishing OpenFootball facts.

    The ledger carries individual observations, but the current schedule
    snapshot is the immutable producer boundary for the OpenFootball rows.
    Replaying it here prevents a bearer-token caller from turning a
    self-declared source URL/hash into a D1 publication receipt.
    """

    if snapshot_path is None or openfootball_raw_archive_dir is None:
        raise ValueError("raw_producer_receipt_required")
    try:
        snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot_value, Mapping):
            raise ValueError("current snapshot root is not an object")
        from league_platform.openfootball_raw_archive import (
            verify_openfootball_snapshot_admission,
        )

        admission = verify_openfootball_snapshot_admission(
            snapshot_value,
            archive_root=openfootball_raw_archive_dir,
        )
        if ledger_observation_identities is not None:
            _verify_openfootball_ledger_rows(
                snapshot_value,
                ledger_observation_identities,
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"raw_producer_receipt_invalid:{exc}") from exc
    return f"verified_current_raw:{admission['admission_sha256']}"


def _verify_openfootball_ledger_rows(
    snapshot: Mapping[str, Any],
    observation_identities: Iterable[str],
) -> None:
    """Bind every OpenFootball ledger row to the replayed raw fixture rows.

    ``verify_openfootball_snapshot_admission`` proves that the current
    snapshot itself was rebuilt from the durable archive.  It does not prove
    that an independently mutable intelligence ledger row came from that
    snapshot.  Rebuild the same fixture-only observation projection and use
    an exact immutable-record comparison; a display name, allowlisted URL or
    hash-shaped value alone must never authorize a publication row.
    """

    fixture_key = "fixture_feed" if "fixture_feed" in snapshot else "espn"
    fixture_section = snapshot.get(fixture_key)
    if not isinstance(fixture_section, Mapping):
        raise ValueError("openfootball_raw_observation_missing")
    # Keep unrelated provider sections out of this binding operation.  The
    # raw admission above has already validated the complete fixture section;
    # this projection only needs the exact OpenFootball fixture observations.
    projection_snapshot: dict[str, Any] = {
        "as_of": snapshot.get("as_of"),
        fixture_key: fixture_section,
    }
    from league_platform.ingestion import snapshot_observations

    expected_identities = {
        _sha256(_canonical_json(row.as_dict()).encode("utf-8"))
        for row in snapshot_observations(projection_snapshot)
        if row.source_name == "OpenFootball"
    }
    if not expected_identities:
        raise ValueError("openfootball_raw_observation_missing")
    found = False
    for identity in observation_identities:
        found = True
        # Observation.__post_init__ already enforces a SHA-256 and HTTPS URL;
        # the exact replay comparison additionally binds those fields and the
        # payload/entity/kind/observed_at to an archive-derived row.
        if identity not in expected_identities:
            raise ValueError("openfootball_ledger_row_unbound")
    if not found:
        raise ValueError("openfootball_raw_observation_missing")


def build_producer_attestation_context(
    *,
    maturity_context: PublicationMaturityContext | None,
    maturity_verification: Mapping[str, Any],
    stream: str,
    rights_use_case: str,
    source_policy_ids: set[str] | tuple[str, ...] | list[str],
    publication_epoch: str,
    key_id: str | None = None,
    signing_secret: str | None = None,
    dry_run: bool,
) -> ProducerAttestationContext | None:
    """Resolve a separate HMAC identity; Bearer credentials never self-attest."""

    if dry_run:
        return None
    if maturity_context is None:
        raise ValueError("publication_maturity_context is required outside dry-run")
    resolved_key_id = (key_id or os.environ.get(PRODUCER_KEY_ID_ENV) or "").strip()
    resolved_secret = signing_secret or os.environ.get(PRODUCER_SIGNING_SECRET_ENV) or ""
    if (
        not resolved_key_id
        or len(resolved_key_id) > 120
        or len(resolved_secret.encode("utf-8")) < 32
        or len(resolved_secret) > 4_096
    ):
        raise ValueError("producer signing identity is required outside dry-run")
    if stream not in {
        "fixture_registration",
        "intelligence_ingest",
        "forecast_registration",
        "forecast_prediction",
        "publication_manifest",
        "audit_only",
    }:
        raise ValueError("producer attestation stream is invalid")
    if rights_use_case not in {"redistribution", "model_input", "audit_only"}:
        raise ValueError("producer attestation rights use case is invalid")
    normalized_source_ids = tuple(
        sorted({str(value).strip() for value in source_policy_ids if str(value).strip()})
    )
    if not normalized_source_ids:
        raise ValueError("producer attestation source policy ids are required")
    receipt_sha = str(maturity_verification.get("receiptSha256") or "").lower()
    if not _SHA256_RE.fullmatch(receipt_sha):
        raise ValueError("producer attestation maturity digest is invalid")
    live_snapshot = maturity_context.artifact_paths.get("live_snapshot")
    if live_snapshot is None:
        raise ValueError("producer attestation live snapshot binding is missing")
    try:
        live_sha = hashlib.sha256(live_snapshot.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("producer attestation live snapshot is unreadable") from exc
    return ProducerAttestationContext(
        key_id=resolved_key_id,
        signing_secret=resolved_secret.encode("utf-8"),
        stream=stream,
        rights_use_case=rights_use_case,
        source_policy_ids=normalized_source_ids,
        publication_epoch=publication_epoch,
        maturity_receipt_raw_sha256=receipt_sha,
        live_snapshot_raw_sha256=live_sha,
    )


def _producer_attestation_headers(
    body: bytes,
    *,
    endpoint: str,
    context: ProducerAttestationContext,
    issued_at: datetime | None = None,
) -> dict[str, str]:
    """Sign the exact UTF-8 request bytes for independent server verification."""

    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires = (now + timedelta(seconds=PRODUCER_ATTESTATION_TTL_SECONDS)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    attestation = {
        "schemaVersion": PRODUCER_ATTESTATION_SCHEMA_VERSION,
        "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
        "keyId": context.key_id,
        "issuedAt": issued,
        "expiresAt": expires,
        "stream": context.stream,
        "rightsUseCase": context.rights_use_case,
        "sourcePolicyIds": list(context.source_policy_ids),
        "publicationEpoch": context.publication_epoch,
        "destination": _normalized_destination_endpoint(endpoint),
        "bodySha256": hashlib.sha256(body).hexdigest(),
        "maturityReceiptRawSha256": context.maturity_receipt_raw_sha256,
        "liveSnapshotRawSha256": context.live_snapshot_raw_sha256,
    }
    canonical = _canonical_json(attestation).encode("utf-8")
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(context.signing_secret, canonical, hashlib.sha256).hexdigest()
    return {
        "X-Matchline-Producer-Attestation": encoded,
        "X-Matchline-Producer-Signature": f"v1={signature}",
    }


def build_publication_maturity_context(
    *,
    receipt_path: Path,
    platform_verification_path: Path,
    strict_report_path: Path,
    prospective_lock_path: Path,
    cycle_path: Path,
    evaluation_path: Path,
    live_snapshot_path: Path,
    offline_snapshot_path: Path,
    sites_build_evidence_path: Path,
    test_evidence_path: Path,
    publication_audit_path: Path,
    prediction_archive_path: Path,
    publication_diagnostic_path: Path,
    runtime_root: Path,
    repository_root: Path,
) -> PublicationMaturityContext:
    """Build the exact twelve raw-artifact bindings required by the gate."""

    return PublicationMaturityContext(
        receipt_path=receipt_path,
        artifact_paths={
            "platform_verification": platform_verification_path,
            "strict_report": strict_report_path,
            "prospective_lock": prospective_lock_path,
            "cycle": cycle_path,
            "evaluation": evaluation_path,
            "live_snapshot": live_snapshot_path,
            "offline_snapshot": offline_snapshot_path,
            "sites_build_evidence": sites_build_evidence_path,
            "test_evidence": test_evidence_path,
            "publication_audit": publication_audit_path,
            "prediction_archive": prediction_archive_path,
            "publication_diagnostic": publication_diagnostic_path,
        },
        runtime_root=runtime_root,
        repository_root=repository_root,
    )


def require_publication_maturity(
    context: PublicationMaturityContext | None,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Revalidate the fresh, artifact-bound maturity receipt for write paths."""

    if dry_run:
        return {
            "status": "audit_only",
            "receiptSha256": "0" * 64,
        }
    if context is None:
        raise ValueError("publication_maturity_context is required outside dry-run")
    try:
        receipt_bytes = context.receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("publication_maturity_receipt is unreadable") from exc
    if not isinstance(receipt, Mapping):
        raise ValueError("publication_maturity_receipt root must be an object")
    # Import lazily so the maturity CLI can itself import the platform evidence
    # graph without creating publisher/runtime cycles.
    from league_platform.maturity_validator import validate_maturity_receipt

    verification = validate_maturity_receipt(
        receipt,
        artifact_paths=context.artifact_paths,
        runtime_root=context.runtime_root,
        repository_root=context.repository_root,
    )
    failures = verification.get("failures")
    if verification.get("status") != "pass" or failures not in ([], ()):  # pragma: no branch - explicit fail-closed shape check
        rendered = (
            ",".join(str(item) for item in failures[:5])
            if isinstance(failures, list)
            else "invalid_verification_shape"
        )
        raise ValueError(f"publication_maturity_blocked:{rendered}")
    return {
        "status": "pass",
        "receiptSha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }

SOURCE_DIAGNOSTIC_PROVIDERS = {
    "sports_lottery_official": "中国体育彩票",
    "espn_schedule_summary": "ESPN",
    "understat_xg": "Understat",
    "official_league_lineups": "Official lineups",
    "laliga_official_match_directory": "LaLiga official match directory",
    "sofascore_prematch": "SofaScore",
    "public_rss_news": "Public RSS",
    "open_meteo": "Open-Meteo",
    "oddstorm_market_comparison": "OddStorm",
    "openligadb_secondary_results": "OpenLigaDB",
    "football_data_historical": "MatchHistory",
    "fbref_public_stats": "FBref",
    "whoscored_public_pages": "WhoScored",
    "clubelo_public_ratings": "ClubElo",
    "sofifa_public_reference": "SoFIFA",
    "crawl4ai_allowlisted_pages": "Crawl4AI 抓取引擎",
    "lazq_public_mirror": "Lazq",
    "fotmob_public_api": "FotMob",
    "laliga_official_news": "LaLiga official news",
    "legacy_anti_waf_relay": "Legacy anti-WAF relay",
}
MAX_SOURCE_DIAGNOSTIC_ROWS = 32

_SOURCE_NAME_TO_POLICY_ID = {
    "OpenFootball": SourceId.OPENFOOTBALL_CURRENT,
    "OpenLigaDB": SourceId.OPENLIGADB_SECONDARY_RESULTS,
    "CFL official": SourceId.CFL_OFFICIAL_CURRENT,
    "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY,
    "Understat": SourceId.UNDERSTAT_XG,
    "Official lineups": SourceId.OFFICIAL_LEAGUE_LINEUPS,
    "Public RSS": SourceId.PUBLIC_RSS_NEWS,
    "Open-Meteo": SourceId.OPEN_METEO_WEATHER,
    "MET Norway Locationforecast": SourceId.MET_NORWAY_WEATHER,
    "Wikidata structured data": SourceId.WIKIDATA_ENTITIES,
    "SofaScore": SourceId.SOFASCORE_PREMATCH,
    "中国体育彩票": SourceId.SPORTS_LOTTERY_OFFICIAL,
    "OddStorm": SourceId.ODDSTORM_MARKET_COMPARISON,
    "FBref": SourceId.FBREF_PUBLIC_STATS,
    "WhoScored": SourceId.WHOSCORED_PUBLIC_PAGES,
    "ClubElo": SourceId.CLUBELO_PUBLIC_RATINGS,
    "SoFIFA": SourceId.SOFIFA_PUBLIC_REFERENCE,
    # Crawl4AI config names are operator-facing labels, not new source
    # identities.  Keep exact aliases for the declared fact-source IDs so a
    # lossless adapter->ledger hand-off reaches the central rights decision;
    # the policy remains the authority and currently blocks every one of
    # these candidate lanes.
    "Football-Data historical match archive operator source": SourceId.FOOTBALL_DATA_HISTORICAL,
    "WhoScored public match centre operator source": SourceId.WHOSCORED_PUBLIC_PAGES,
    "Premier League official public match pages operator source": SourceId.PREMIER_LEAGUE_PUBLIC_PAGES,
    "LaLiga official public match pages operator source": SourceId.LALIGA_PUBLIC_PAGES,
    "LaLiga official public news operator source": SourceId.LALIGA_OFFICIAL_NEWS,
    "Bundesliga official public match pages operator source": SourceId.BUNDESLIGA_PUBLIC_PAGES,
    "Serie A official public match pages operator source": SourceId.SERIEA_PUBLIC_PAGES,
    "Ligue 1 official public news operator source": SourceId.LIGUE1_OFFICIAL_NEWS,
}

# Crawl4AI is a transport/runtime, not a redistributable fact source.  These
# markers are copied into the immutable observation payload by ingestion and
# must therefore be checked again immediately before a publisher request.  A
# ledger row can be hand-written or inherited from an older runtime; source
# rights cannot be inferred from the top-level display name alone.
_CRAWL4AI_CAPTURE_MARKER_KEYS = frozenset(
    {
        "capture_engine",
        "captureEngine",
        "capture_role",
        "captureRole",
    }
)
_CRAWL4AI_CAPTURE_ENGINE = "crawl4ai"
_CRAWL4AI_CAPTURE_ROLE = "fetch_runtime"
_CRAWL4AI_SOURCE_ROLE = "fact_source_capture"
_CRAWL4AI_EXECUTION_MARKER_KEYS = frozenset(
    {
        "provider_role",
        "providerRole",
        "execution_layer",
        "executionLayer",
    }
)
_CRAWL4AI_EXECUTION_ROLES = frozenset(
    {
        "execution_layer",
        "crawl4ai",
        "fetch_runtime",
    }
)
_CRAWL4AI_MARKER_CONTAINERS = frozenset(
    {
        "adapter",
        "execution",
        "metadata",
        "policy",
        "provenance",
        "runtime",
    }
)
_CRAWL4AI_MARKER_MAX_DEPTH = 3
_CRAWL4AI_OPENFOOTBALL_IDS = frozenset(
    {
        SourceId.OPENFOOTBALL_CURRENT.value,
        SourceId.OPENFOOTBALL_HISTORICAL.value,
    }
)


def _crawl4ai_payload_value(payload: Mapping[str, object], *keys: str) -> object:
    """Read one bounded identity field from an observation payload.

    ``metadata`` is included for older ledger rows that nested the runtime
    envelope there.  It is deliberately only a lookup location: no value in
    this untrusted payload can grant source rights.
    """

    for key in keys:
        if key in payload:
            return payload[key]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        for key in keys:
            if key in metadata:
                return metadata[key]
    return None


def _crawl4ai_marker_values(
    payload: Mapping[str, object],
    *keys: str,
) -> Iterator[object]:
    """Yield marker values from bounded runtime/provenance envelopes.

    Ledger payloads are untrusted JSON.  Only the named envelope containers
    are traversed, and the depth cap prevents a crafted object from turning a
    publication preflight into an unbounded recursive walk.  This helper is
    intentionally separate from ``_crawl4ai_payload_value``: identity and
    rights fields keep their historical top-level/metadata lookup semantics,
    while execution markers cannot hide one level deeper in an adapter
    envelope.
    """

    pending: list[tuple[Mapping[str, object], int]] = [(payload, 0)]
    visited: set[int] = set()
    wanted = frozenset(keys)
    while pending:
        current, depth = pending.pop(0)
        marker_id = id(current)
        if marker_id in visited:
            continue
        visited.add(marker_id)
        for key in wanted:
            if key in current:
                yield current[key]
        if depth >= _CRAWL4AI_MARKER_MAX_DEPTH:
            continue
        for container in _CRAWL4AI_MARKER_CONTAINERS:
            nested = current.get(container)
            if isinstance(nested, Mapping):
                pending.append((nested, depth + 1))


def _normalised_identity_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text or None


def _is_crawl4ai_observation(observation: Observation) -> bool:
    """Detect runtime/page provenance before applying the provider name map."""

    if observation.kind.strip().casefold().startswith("crawl4ai_"):
        return True
    payload = observation.payload
    if not isinstance(payload, Mapping):
        return False
    # Presence of a capture marker is itself enough to enter the strict lane;
    # malformed values must not evade the gate by failing normalization.
    if any(
        True
        for key in _CRAWL4AI_CAPTURE_MARKER_KEYS
        for _value in _crawl4ai_marker_values(payload, key)
    ):
        return True
    engines = {
        _normalised_identity_text(value)
        for value in _crawl4ai_marker_values(
            payload, "capture_engine", "captureEngine", "provider"
        )
    }
    roles = {
        _normalised_identity_text(value)
        for value in _crawl4ai_marker_values(payload, "capture_role", "captureRole")
    }
    source_roles = {
        _normalised_identity_text(value)
        for value in _crawl4ai_marker_values(payload, "source_role", "sourceRole")
    }
    execution_roles = {
        _normalised_identity_text(value)
        for value in _crawl4ai_marker_values(
            payload, *_CRAWL4AI_EXECUTION_MARKER_KEYS
        )
    }
    return bool(
        _CRAWL4AI_CAPTURE_ENGINE in engines
        or _CRAWL4AI_CAPTURE_ROLE in roles
        or _CRAWL4AI_SOURCE_ROLE in source_roles
        or bool(execution_roles & _CRAWL4AI_EXECUTION_ROLES)
    )


def _crawl4ai_observation_rights_reason(
    observation: Observation,
    policy_id: SourceId | None,
) -> str | None:
    """Fail closed on runtime identity before a page can reach publication."""

    payload = observation.payload
    if not isinstance(payload, Mapping):
        return "crawl4ai_identity_missing"

    test_only = _crawl4ai_payload_value(payload, "test_only", "testOnly")
    if test_only is True or (test_only is not None and not isinstance(test_only, bool)):
        return "crawl4ai_test_only_blocked"
    if observation.enters_model:
        return "crawl4ai_model_admission_blocked"

    source_id = _crawl4ai_payload_value(payload, "source_id", "sourceId")
    fact_source_id = _crawl4ai_payload_value(
        payload,
        "fact_source_id",
        "factSourceId",
    )
    if not isinstance(source_id, str) or not source_id.strip():
        return "crawl4ai_identity_missing"
    if not isinstance(fact_source_id, str) or not fact_source_id.strip():
        return "crawl4ai_identity_missing"
    source_id = source_id.strip()
    fact_source_id = fact_source_id.strip()
    if source_id != fact_source_id:
        return "crawl4ai_source_identity_mismatch"
    if policy_id is None or source_id != policy_id.value:
        return "crawl4ai_source_identity_mismatch"
    if policy_id.value in _CRAWL4AI_OPENFOOTBALL_IDS:
        return "crawl4ai_openfootball_relabel_blocked"

    # A runtime page is publishable only after the adapter's robots decision
    # has been captured.  Missing/false/ambiguous declarations are not proof of
    # permission and cannot be upgraded by the publisher.
    robots = _crawl4ai_payload_value(payload, "robots")
    if not isinstance(robots, Mapping) or robots.get("allowed") is not True:
        return "crawl4ai_robots_unverified"

    # The browser adapter marks these fields false for every page.  A true or
    # malformed self-attestation is a provenance conflict, not an entitlement.
    for key in ("model_eligible", "modelEligible", "allow_model", "allowModel"):
        value = _crawl4ai_payload_value(payload, key)
        if value is True:
            return "crawl4ai_model_admission_blocked"
    runtime_evidence = _crawl4ai_payload_value(
        payload,
        "runtime_evidence",
        "runtimeEvidence",
    )
    # A runtime receipt is evidence only when it is the literal JSON boolean
    # true.  Omission, null, strings, numbers, and false are not proof that a
    # real execution-layer capture occurred.
    if runtime_evidence is not True:
        return "crawl4ai_runtime_evidence_missing"
    return None


def _read_json_object(path: Path | None) -> dict:
    """Read a small evidence object without making publication depend on it."""

    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def build_prospective_gate_metadata(
    *,
    lock_path: Path | None = None,
    evaluation_path: Path | None = None,
    selection_path: Path | None = None,
    strict_report_path: Path | None = None,
    cycle_path: Path | None = None,
) -> dict | None:
    """Project bounded prospective evidence into source-run audit metadata.

    This is a status projection, not a second prediction store. Raw metrics,
    failure lists and model files stay local; only fields the Sites read model
    needs to explain the gate are transported.
    """

    lock = _read_json_object(lock_path)
    evaluation = _read_json_object(evaluation_path)
    selection = _read_json_object(selection_path)
    strict = _read_json_object(strict_report_path)
    cycle = _read_json_object(cycle_path)
    overall: Mapping[str, Any] = (
        strict["overall"] if isinstance(strict.get("overall"), Mapping) else {}
    )
    if not any((lock, evaluation, selection, overall, cycle)):
        return None

    def bounded_text(value: object, length: int = 240) -> str | None:
        return _bounded_diagnostic_text(value, length)

    def bounded_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and math.isfinite(value):
            return max(0, int(value))
        return None

    def optional_bool(value: object) -> bool | None:
        return value if isinstance(value, bool) else None

    return {
        "schemaVersion": "matchline.prospective-gate-diagnostics.v1",
        "status": bounded_text(evaluation.get("status") or lock.get("status"), 80),
        "modelVersionSha256": bounded_text(lock.get("model_version_sha256"), 128),
        "freezeModelName": bounded_text(lock.get("freeze_model_name"), 180),
        "lockedAt": bounded_text(lock.get("locked_at"), 80),
        "evaluationWindowStartedAt": bounded_text(lock.get("evaluation_window_started_at"), 80),
        "scoredN": bounded_int(evaluation.get("scored_n")),
        "pendingN": bounded_int(evaluation.get("pending_n")),
        "resultConflicts": bounded_int(evaluation.get("result_conflicts")),
        "sampleRequirementsMet": optional_bool(evaluation.get("sample_requirements_met")),
        "allRequiredTargetsScored": optional_bool(evaluation.get("all_required_targets_scored")),
        "predictionFreezesVerified": optional_bool(evaluation.get("prediction_freezes_verified")),
        "selectionGateStatus": bounded_text(selection.get("gate_status"), 100),
        "selectionProductionEligible": optional_bool(selection.get("production_eligible")),
        "marketGate": bounded_text(overall.get("market_gate"), 120),
        "freezeStageGate": bounded_text(overall.get("freeze_stage_gate"), 120),
        "modelSelectionGate": bounded_text(overall.get("model_selection_gate"), 120),
        "cycleStatus": bounded_text(cycle.get("status"), 80),
        "cycleAsOf": bounded_text((cycle.get("sync") or {}).get("as_of") if isinstance(cycle.get("sync"), Mapping) else None, 80),
        "modelUse": "display_only_audit_metadata",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # noqa: D401
        self,
        request: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ValueError("ingest endpoint redirected; refusing to follow it")


class IngestPublishError(RuntimeError):
    """An ingest failure with an explicit retry classification."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_PUBLICATION_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def resolve_publication_epoch(value: str | None, *, dry_run: bool) -> str:
    """Return the bounded deployment identity used by receipt journals."""

    if value is None or not value.strip():
        if dry_run:
            return "dry-run"
        raise ValueError("publication_epoch is required outside dry-run")
    normalized = value.strip()
    if not _PUBLICATION_EPOCH_RE.fullmatch(normalized):
        raise ValueError("publication_epoch has an invalid format")
    return normalized


def _normalized_destination_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("publication destination must be an HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("publication destination has an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))


def publication_scope(
    *,
    stream: str,
    destinations: Mapping[str, str],
    publication_epoch_value: str,
    publication_gate_sha256: str,
) -> dict[str, object]:
    """Build the endpoint/epoch identity shared by receipts and cursors."""

    if not stream or len(stream) > 80:
        raise ValueError("publication receipt stream is invalid")
    normalized_destinations = {
        str(role): _normalized_destination_endpoint(endpoint)
        for role, endpoint in sorted(destinations.items())
        if str(role)
    }
    if len(normalized_destinations) != len(destinations) or not normalized_destinations:
        raise ValueError("publication destinations are invalid")
    normalized_gate_sha = publication_gate_sha256.lower()
    if not _SHA256_RE.fullmatch(normalized_gate_sha):
        raise ValueError("publication gate digest is invalid")
    document = {
        "schemaVersion": PUBLICATION_RECEIPT_SCHEMA_VERSION,
        "stream": stream,
        "destinations": normalized_destinations,
        "publicationEpoch": publication_epoch_value,
        "publicationGateSha256": normalized_gate_sha,
    }
    return {
        **document,
        "publicationScopeSha256": _sha256(
            _canonical_json(document).encode("utf-8")
        ),
    }


def publication_receipt_identity(
    *,
    stream: str,
    destinations: Mapping[str, str],
    publication_epoch_value: str,
    publication_gate_sha256: str,
    outbound_payload: object,
) -> dict[str, object]:
    """Bind one completed receipt to its full outbound schema and destination."""

    scope = publication_scope(
        stream=stream,
        destinations=destinations,
        publication_epoch_value=publication_epoch_value,
        publication_gate_sha256=publication_gate_sha256,
    )
    outbound_digest = _sha256(_canonical_json(outbound_payload).encode("utf-8"))
    identity_document = {
        **scope,
        "outboundPayloadSha256": outbound_digest,
    }
    return {
        **identity_document,
        "receiptIdentitySha256": _sha256(
            _canonical_json(identity_document).encode("utf-8")
        ),
    }


def _ledger_prefix_digest(path: Path, length: int) -> str:
    with path.open("rb") as stream:
        return _sha256(stream.read(max(0, min(length, LEDGER_CURSOR_PREFIX_BYTES))))


def _default_cursor_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f"{ledger_path.name}.sites-cursor.json")


def _load_ledger_cursor(
    ledger_path: Path,
    cursor_path: Path,
    *,
    publication_scope_sha256: str,
) -> dict[str, int]:
    """Return a safe append offset, or zero when the ledger identity changed."""

    try:
        value = json.loads(cursor_path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("schemaVersion") != LEDGER_CURSOR_SCHEMA_VERSION
            or value.get("publicationScopeSha256") != publication_scope_sha256
        ):
            return {"offset": 0, "completed_batches": 0}
        offset = value.get("offset")
        size = ledger_path.stat().st_size
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > size:
            return {"offset": 0, "completed_batches": 0}
        prefix_length = value.get("prefixLength")
        if (
            not isinstance(prefix_length, int)
            or isinstance(prefix_length, bool)
            or prefix_length < 0
            or prefix_length > LEDGER_CURSOR_PREFIX_BYTES
            or prefix_length > offset
            or value.get("prefixSha256") != _ledger_prefix_digest(ledger_path, prefix_length)
        ):
            return {"offset": 0, "completed_batches": 0}
        completed_batches = value.get("completedBatches", 0)
        if not isinstance(completed_batches, int) or isinstance(completed_batches, bool) or completed_batches < 0:
            completed_batches = 0
        return {"offset": offset, "completed_batches": completed_batches}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"offset": 0, "completed_batches": 0}


def _save_ledger_cursor(
    ledger_path: Path,
    cursor_path: Path,
    offset: int,
    *,
    completed_batches: int,
    publication_scope_sha256: str,
) -> None:
    """Atomically persist a cursor only after the current publication scan completes."""

    size = ledger_path.stat().st_size
    if offset < 0 or offset > size:
        raise ValueError("ledger cursor offset is outside the ledger")
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": LEDGER_CURSOR_SCHEMA_VERSION,
        "ledgerSize": size,
        "offset": offset,
        "prefixLength": min(offset, LEDGER_CURSOR_PREFIX_BYTES),
        "prefixSha256": _ledger_prefix_digest(ledger_path, min(offset, LEDGER_CURSOR_PREFIX_BYTES)),
        "completedBatches": completed_batches,
        "publicationScopeSha256": publication_scope_sha256,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    temporary = cursor_path.with_name(f".{cursor_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
        temporary.replace(cursor_path)
    finally:
        temporary.unlink(missing_ok=True)


def _redact(value: object, token: str = "") -> str:
    text = str(value)
    if token:
        text = text.replace(token, "[REDACTED]")
    return text[:4096]


def _sites_bypass_token() -> str:
    """Read the optional Sites dispatch token without allowing header injection."""

    value = os.environ.get(SITES_BYPASS_TOKEN_ENV, "").strip()
    if not value:
        return ""
    if len(value) > 1024 or "\r" in value or "\n" in value:
        raise ValueError(f"{SITES_BYPASS_TOKEN_ENV} must be a bounded single-line token")
    return value


def _publish_user_agent() -> str:
    """Return a bounded operator override or the stable Sites-compatible UA."""

    value = os.environ.get(PUBLISH_USER_AGENT_ENV, DEFAULT_PUBLISH_USER_AGENT).strip()
    if not value or len(value) > 240 or "\r" in value or "\n" in value:
        raise ValueError(f"{PUBLISH_USER_AGENT_ENV} must be a bounded single-line value")
    return value


def _iso_bounds(rows: list[dict]) -> tuple[str, str]:
    times = [utc(row["observed_at"]) for row in rows]
    if not times:
        now = datetime.now(timezone.utc).isoformat()
        return now, now
    return min(times).isoformat(), max(times).isoformat()


def _normalise_observation_cutoff(value: datetime | str | None) -> datetime | None:
    """Normalize an optional prospective publication cutoff to UTC."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, str):
        return utc(value)
    raise ValueError("min_observed_at must be an ISO timestamp or datetime")


def _source_run_provider(rows: list[dict], fallback: str = "Matchline local ledger") -> str:
    """Return a truthful provider label for one ingest batch.

    The local ledger is append-only and contains observations from several
    public sources.  Labelling every D1 run as ``Matchline local ledger``
    hides source freshness in the Sites source-health panel.  Batches are
    normally source-contiguous, so preserve the actual ``source_name`` when
    there is exactly one distinct value.  A boundary batch can contain more
    than one source; keep that fact explicit instead of pretending that one
    provider produced all rows.
    """

    names = sorted({
        str(row.get("source_name") or "").strip()
        for row in rows
        if str(row.get("source_name") or "").strip()
    })
    if len(names) == 1 and len(names[0]) <= 120:
        return names[0]
    if len(names) > 1:
        return f"{fallback} ({len(names)} sources)"[:120]
    return fallback


def _wire_entity_id(row: Mapping[str, object]) -> str:
    """Return an ingest-safe entity ID without changing the local ledger.

    News collectors historically used the article URL as ``entity_id``. Some
    Google News and media URLs exceed the Sites API's 160-character entity ID
    bound even though the source URL and payload remain valid provenance. Keep
    short IDs verbatim; for long news URLs use a deterministic bounded key and
    retain the original URL in the immutable source/payload fields. Other
    overlong IDs are rejected rather than silently truncated.
    """

    value = row.get("entity_id")
    if not isinstance(value, str) or not value:
        raise ValueError("entity_id must be a non-empty string")
    if len(value) <= 160:
        return value
    if row.get("entity_type") == "news":
        return f"news-url-sha256:{_sha256(value.encode('utf-8'))}"
    raise ValueError("entity_id exceeds 160 characters")


def _wire_payload(value: object, *, path: str = "payload", depth: int = 0) -> object:
    """Project a local payload into the bounded Sites JSON contract.

    The append-only ledger remains byte-for-byte untouched. Oversized text is
    represented by a bounded prefix plus its original byte length and digest,
    so the D1 display row is explicit about lossiness and can be joined back to
    the source archive. Structural violations remain fail-closed.
    """

    if depth > MAX_WIRE_PAYLOAD_DEPTH:
        raise ValueError(f"{path} exceeds maximum nesting depth of {MAX_WIRE_PAYLOAD_DEPTH}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return value
    if isinstance(value, str):
        if len(value) <= MAX_WIRE_PAYLOAD_STRING_LENGTH:
            return value
        digest = _sha256(value.encode("utf-8"))
        marker = f"[matchline-truncated sha256={digest} original_length={len(value)}]\n"
        return marker + value[: max(0, MAX_WIRE_PAYLOAD_STRING_LENGTH - len(marker))]
    if isinstance(value, list):
        if len(value) > MAX_WIRE_PAYLOAD_ARRAY_ITEMS:
            raise ValueError(f"{path} array exceeds {MAX_WIRE_PAYLOAD_ARRAY_ITEMS} items")
        return [_wire_payload(item, path=f"{path}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if len(value) > MAX_WIRE_PAYLOAD_KEYS:
            raise ValueError(f"{path} object exceeds {MAX_WIRE_PAYLOAD_KEYS} keys")
        projected = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 160:
                raise ValueError(f"{path} contains an invalid key")
            projected[key] = _wire_payload(item, path=f"{path}.{key}", depth=depth + 1)
        return projected
    raise ValueError(f"{path} contains an unsupported value")


def build_ingest_payload(
    rows: list[dict],
    *,
    provider: str | None = None,
    source_type: str = "intelligence_jsonl",
    metadata: Mapping[str, object] | None = None,
) -> dict:
    """Build the wire payload without adding or rewriting observation fields.

    ``metadata`` is a bounded display/operations envelope for source-run
    diagnostics. It is never copied into an observation and cannot authorize
    a model feature.
    """

    if len(rows) > MAX_OBSERVATIONS_PER_REQUEST:
        raise ValueError(f"at most {MAX_OBSERVATIONS_PER_REQUEST} observations per request")
    started_at, finished_at = _iso_bounds(rows)
    observations = []
    for row in rows:
        observations.append(
            {
                # Preserve canonical IDs when an upstream enrichment has
                # already resolved them.  Older ledger rows do not carry
                # these keys, so omission remains backward-compatible while
                # preventing a resolved observation from becoming detached
                # from its D1 fixture/team/player on the wire.
                **({"fixtureId": row["fixture_id"]} if row.get("fixture_id") is not None else {}),
                **({"teamId": row["team_id"]} if row.get("team_id") is not None else {}),
                **({"playerId": row["player_id"]} if row.get("player_id") is not None else {}),
                "entityType": row["entity_type"],
                "entityId": _wire_entity_id(row),
                "kind": row["kind"],
                "payload": _wire_payload(row["payload"]),
                "sourceName": row["source_name"],
                "sourceUrl": row["source_url"],
                "sourceTier": row["source_tier"],
                "effectiveAt": row.get("effective_at"),
                "observedAt": row["observed_at"],
                "confidence": row["confidence"],
                "rawHash": row["raw_hash"],
                "entersModel": bool(row.get("enters_model", False)),
            }
        )
    source_run = {
        "provider": provider or _source_run_provider(rows),
        "sourceType": source_type,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "status": "ok",
        "recordsSeen": len(rows),
    }
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise ValueError("source-run metadata must be an object")
        try:
            encoded_metadata = _canonical_json(dict(metadata)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("source-run metadata must be JSON serializable") from exc
        if len(encoded_metadata) > MAX_SOURCE_RUN_METADATA_BYTES:
            raise ValueError("source-run metadata exceeds 64 KiB")
        source_run["metadata"] = dict(metadata)
    return {
        "sourceRun": source_run,
        "observations": observations,
    }


def build_lineup_diagnostics_payload(snapshot: Mapping[str, object]) -> dict:
    """Build an empty-observation D1 run carrying bounded lineup diagnostics."""

    raw = snapshot.get("lineup_poll_diagnostics")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("snapshot has no lineup_poll_diagnostics object")
    if any(
        not isinstance(competition_id, str) or not isinstance(row, Mapping)
        for competition_id, row in raw.items()
    ):
        raise ValueError("lineup_poll_diagnostics contains an invalid row")

    diagnostics: dict[str, dict[str, object]] = {}
    count_fields = {
        "candidate_count": "candidateCount",
        "invalid_kickoff_count": "invalidKickoffCount",
        "source_lineup_count": "sourceLineupCount",
        "confirmed_lineup_count": "confirmedLineupCount",
        "model_eligible_lineup_count": "modelEligibleLineupCount",
        "error_count": "errorCount",
    }
    for competition_id, raw_row in raw.items():
        if not isinstance(competition_id, str) or not isinstance(raw_row, Mapping):
            raise ValueError("lineup_poll_diagnostics contains an invalid row")
        row: dict[str, object] = {
            "state": _bounded_diagnostic_text(raw_row.get("state"), 60)
            or "unknown",
            "sourceStatus": _bounded_diagnostic_text(
                raw_row.get("source_status"), 60
            )
            or "unknown",
            "reasonCode": _bounded_diagnostic_text(
                raw_row.get("reason_code"), 120
            ),
            "requested": raw_row.get("requested") is True,
        }
        for source_key, target_key in count_fields.items():
            value = raw_row.get(source_key)
            row[target_key] = (
                min(value, 10_000_000)
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else 0
            )
        if isinstance(raw_row.get("due"), bool):
            row["pollDue"] = raw_row["due"]
        diagnostics[competition_id] = row
    as_of = utc(str(snapshot.get("as_of"))).isoformat()
    states = {str(row.get("state")) for row in diagnostics.values()}
    degraded_states = {"source_unavailable", "not_configured", "partial"}
    status = "degraded" if states & degraded_states else "ok"
    metadata = {
        "schemaVersion": "matchline.lineup-poll-diagnostics.v1",
        "asOf": as_of,
        "diagnostics": diagnostics,
        "modelUse": "display_only_audit_metadata",
    }
    payload = build_ingest_payload(
        [],
        provider="Matchline lineup poll",
        source_type="lineup_poll_diagnostics",
        metadata=metadata,
    )
    payload["sourceRun"].update({
        "startedAt": as_of,
        "finishedAt": as_of,
        "status": status,
        "recordsSeen": len(diagnostics),
        "errorCode": "lineup_coverage_incomplete" if status == "degraded" else None,
    })
    return payload


def _bounded_diagnostic_text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _diagnostic_error_code(error: Mapping[str, object]) -> str | None:
    explicit = _bounded_diagnostic_text(error.get("error_code"), 80)
    if explicit:
        return explicit
    message = (_bounded_diagnostic_text(error.get("error"), 240) or "").lower()
    match = re.search(r"(?:http(?:s)?\s*error\s*|status\s*)(4\d\d|5\d\d)", message)
    if match:
        return f"http_{match.group(1)}"
    if any(token in message for token in ("tls", "ssl", "certificate", "handshake")):
        return "tls_error"
    if any(token in message for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in message for token in ("network", "connection", "socket", "dns")):
        return "network_error"
    return None


def _source_diagnostic_provider(row: Mapping[str, object]) -> str:
    source_id = row.get("id")
    if isinstance(source_id, str) and source_id in SOURCE_DIAGNOSTIC_PROVIDERS:
        return SOURCE_DIAGNOSTIC_PROVIDERS[source_id]
    name = _bounded_diagnostic_text(row.get("name"), 120)
    if name:
        return name
    raise ValueError("source registry row has no provider label")


def _source_diagnostic_runtime(row: Mapping[str, object]) -> dict[str, object]:
    """Project transport health only; never copy source facts into audit metadata."""

    raw_runtime = row.get("runtime")
    runtime = raw_runtime if isinstance(raw_runtime, Mapping) else {}
    raw_errors = runtime.get("errors")
    error_codes: list[str] = []
    if isinstance(raw_errors, list):
        for error in raw_errors[:5]:
            if not isinstance(error, Mapping):
                continue
            error_code = _diagnostic_error_code(error)
            if error_code and error_code not in error_codes:
                error_codes.append(error_code)
    record_count = runtime.get("record_count", 0)
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        record_count = 0
    extracted_record_count = runtime.get("extracted_record_count")
    if (
        not isinstance(extracted_record_count, int)
        or isinstance(extracted_record_count, bool)
        or extracted_record_count < 0
    ):
        extracted_record_count = None
    error_count = runtime.get("error_count", len(error_codes))
    if not isinstance(error_count, int) or isinstance(error_count, bool) or error_count < 0:
        error_count = len(error_codes)
    result: dict[str, object] = {
        "status": _bounded_diagnostic_text(runtime.get("status"), 60) or "unknown",
        "retrievedAt": _bounded_diagnostic_text(runtime.get("retrieved_at"), 80),
        "recordCount": min(record_count, 10_000_000),
        "errorCount": min(error_count, 10_000_000),
        "errorCodes": error_codes,
    }
    if extracted_record_count is not None:
        result["extractedRecordCount"] = min(extracted_record_count, 10_000_000)
    execution = runtime.get("execution")
    if isinstance(execution, Mapping):
        execution_payload: dict[str, object] = {}
        for source_key, target_key in (
            ("host_group_count", "hostGroupCount"),
            ("max_host_workers", "maxHostWorkers"),
            ("configured_source_count", "configuredSourceCount"),
            ("index_page_count", "indexPageCount"),
            ("detail_page_count", "detailPageCount"),
            ("follow_up_candidate_count", "followUpCandidateCount"),
            ("follow_up_requested_count", "followUpRequestedCount"),
            ("follow_up_rejected_count", "followUpRejectedCount"),
            ("follow_up_capped_count", "followUpCappedCount"),
        ):
            value = execution.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                limit = 100 if source_key in {"host_group_count", "max_host_workers", "configured_source_count"} else 10_000_000
                execution_payload[target_key] = min(value, limit)
        duration_ms = execution.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and math.isfinite(float(duration_ms)) and duration_ms >= 0:
            execution_payload["durationMs"] = round(min(float(duration_ms), 86_400_000.0), 3)
        if execution_payload:
            result["executionCounts"] = execution_payload
    return result


def _source_diagnostic_status(policy_status: str, runtime_status: str) -> tuple[str, str | None]:
    """Map registry policy/runtime states to the source-run display contract."""

    status = runtime_status or policy_status or "unknown"
    if status == "fresh":
        return "ok", None
    if status == "degraded":
        return "degraded", None
    if status in {"unavailable", "failed"}:
        return "failed", None
    if status in {
        "outside_horizon",
        "not_requested",
        "not_configured",
        "dependency_missing",
        "blocked_by_robots",
        "quarantined",
        "opt_in",
        "forbidden",
        "training_only",
    }:
        codes = {
            "outside_horizon": "outside_horizon",
            "not_requested": "source_not_requested",
            "not_configured": "source_not_configured",
            "dependency_missing": "crawl4ai_dependency_missing",
            "blocked_by_robots": "robots_denied",
            "quarantined": "source_quarantined",
            "opt_in": "source_opt_in_required",
            "forbidden": "source_forbidden",
            "training_only": "training_only",
        }
        return status, codes[status]
    return "degraded", "source_status_unknown"


def build_source_health_payloads(
    snapshot: Mapping[str, object],
    *,
    prospective_gate_metadata: Mapping[str, object] | None = None,
) -> list[dict]:
    """Build one bounded, empty-observation run for every registered source.

    The snapshot's declarative registry is the source of truth for policy and
    runtime state. Each row is published separately so a D1/source-health
    failure can be isolated to one provider, including quarantined or
    robots-blocked candidates that have no observations.
    """

    raw_rows = snapshot.get("source_registry")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("snapshot has no source_registry rows")
    if len(raw_rows) > MAX_SOURCE_DIAGNOSTIC_ROWS:
        raise ValueError("source registry exceeds diagnostic row limit")
    as_of = utc(str(snapshot.get("as_of"))).isoformat()
    payloads: list[dict] = []
    seen_ids: set[str] = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise ValueError("source registry contains an invalid row")
        source_id = raw_row.get("id")
        if not isinstance(source_id, str) or not source_id.strip() or source_id in seen_ids:
            raise ValueError("source registry ids must be unique strings")
        seen_ids.add(source_id)
        provider = _source_diagnostic_provider(raw_row)
        policy_status = _bounded_diagnostic_text(raw_row.get("status"), 60) or "unknown"
        runtime = _source_diagnostic_runtime(raw_row)
        runtime_status = str(runtime["status"])
        run_status, default_error_code = _source_diagnostic_status(policy_status, runtime_status)
        runtime_error_count = runtime.get("errorCount")
        if (
            isinstance(runtime_error_count, int)
            and runtime_error_count > 0
            and run_status in {"ok", "not_requested", "outside_horizon"}
        ):
            # A source with a transport/parser error is never presented as a
            # healthy or merely idle run, even if its registry adapter also
            # reports that it did not request a record window.
            run_status = "degraded"
        raw_runtime_error_codes = runtime.get("errorCodes")
        runtime_error_codes: list[str] = [
            value
            for value in (
                raw_runtime_error_codes
                if isinstance(raw_runtime_error_codes, list)
                else []
            )
            if isinstance(value, str)
        ]
        error_code = (
            _bounded_diagnostic_text(runtime_error_codes[0], 120)
            if runtime_error_codes
            else None
        )
        metadata = {
            "schemaVersion": "matchline.source-health-diagnostics.v1",
            "asOf": as_of,
            "sourceId": source_id,
            "sourceName": _bounded_diagnostic_text(raw_row.get("name"), 160) or provider,
            "policyStatus": policy_status,
            "sourceTier": _bounded_diagnostic_text(raw_row.get("source_tier"), 80),
            "sourceRole": _bounded_diagnostic_text(raw_row.get("role"), 40) or "fact_source",
            "factSource": raw_row.get("fact_source") is not False,
            "captureEngine": "Crawl4AI" if raw_row.get("adapter") == "league_platform.live_sources.crawl4ai" else None,
            "accessPolicy": _bounded_diagnostic_text(raw_row.get("access_policy"), 160),
            "modelPolicy": _bounded_diagnostic_text(raw_row.get("model_policy"), 240),
            "runtime": runtime,
            "modelUse": "display_only_audit_metadata",
        }
        # ``prospective_gate_metadata`` is intentionally not copied into this
        # audit-only lane. Model/evaluation facts have their own authenticated
        # evidence artifacts and would turn source health into a laundering
        # path for material prediction data.
        payload = build_ingest_payload(
            [],
            provider=provider,
            source_type="source_health_diagnostics",
            metadata=metadata,
        )
        payload["sourceRun"].update({
            "startedAt": as_of,
            "finishedAt": as_of,
            "status": run_status,
            "recordsSeen": runtime.get("extractedRecordCount", runtime["recordCount"]),
            "errorCode": error_code or default_error_code,
        })
        payloads.append(payload)
    return payloads


def _validate_endpoint(endpoint: str, token: str, *, label: str = "MATCHLINE_INGEST_URL") -> None:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTPS URL")
    if not token:
        raise ValueError("MATCHLINE_INGEST_TOKEN is required")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(MAX_RETRY_DELAY_SECONDS, float(value)))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay = parsed.timestamp() - datetime.now(timezone.utc).timestamp()
        return max(0.0, min(MAX_RETRY_DELAY_SECONDS, delay))
    except (TypeError, ValueError, OverflowError):
        return None


def _http_error_message(exc: urllib.error.HTTPError, token: str, label: str) -> str:
    try:
        detail = exc.read(4096).decode("utf-8", errors="replace")
    except OSError:
        detail = "response body unavailable"
    return _redact(f"{label} HTTP {exc.code}: {detail}", token)


def _publish_once(
    payload: dict,
    *,
    endpoint: str,
    token: str,
    timeout: float,
    label: str,
    producer_attestation: ProducerAttestationContext | None,
    independent_verifier_headers: Mapping[str, str] | None = None,
) -> dict:
    body = _canonical_json(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _publish_user_agent(),
    }
    # Sites' public edge can reject non-browser publisher signatures before
    # the request reaches the Worker.  The optional dispatch token is issued
    # by Sites for the operator's own project and is never persisted here.
    bypass_token = _sites_bypass_token()
    if bypass_token:
        headers["OAI-Sites-Authorization"] = f"Bearer {bypass_token}"
    if producer_attestation is None:
        raise ValueError("producer attestation is required for protected publication")
    if independent_verifier_headers:
        if producer_attestation.stream != "publication_manifest":
            raise ValueError("independent verifier headers require publication_manifest stream")
        allowed_header_names = {
            "X-Matchline-Independent-Verifier-Proof",
            "X-Matchline-Independent-Verifier-Signature",
        }
        if set(independent_verifier_headers) != allowed_header_names:
            raise ValueError("independent verifier headers are incomplete")
        if any(
            not isinstance(value, str) or not value
            for value in independent_verifier_headers.values()
        ):
            raise ValueError("independent verifier headers are malformed")
        headers.update(independent_verifier_headers)
    headers.update(
        _producer_attestation_headers(
            body,
            endpoint=endpoint,
            context=producer_attestation,
        )
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("ingest response exceeded 1 MiB")
            decoded = json.loads(raw.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("ingest response must be a JSON object")
            return decoded
    except urllib.error.HTTPError as exc:
        retryable = exc.code in TRANSIENT_HTTP_STATUS or exc.code >= 500
        raise IngestPublishError(
            _http_error_message(exc, token, label),
            status_code=exc.code,
            retryable=retryable,
            retry_after=_retry_after_seconds(exc.headers.get("Retry-After")),
        ) from exc
    except urllib.error.URLError as exc:
        # A transport failure has no HTTP status and is safe to retry with a
        # bounded attempt count.  Do not include the full request or token in
        # the diagnostic.
        reason = getattr(exc, "reason", "transport error")
        raise IngestPublishError(
            f"{label} transport failure: {_redact(reason, token)}", retryable=True
        ) from exc
    except (TimeoutError, socket.timeout, OSError) as exc:
        raise IngestPublishError(
            f"{label} transport failure: {_redact(exc, token)}", retryable=True
        ) from exc


def publish_payload(
    payload: dict,
    *,
    endpoint: str,
    token: str,
    timeout: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    error_label: str = "ingest",
    producer_attestation: ProducerAttestationContext | None = None,
    independent_verifier_headers: Mapping[str, str] | None = None,
) -> dict:
    """POST one payload, retrying only classified transient failures."""

    _validate_endpoint(endpoint, token, label=f"{error_label} endpoint")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds must be non-negative")

    for attempt in range(max_retries + 1):
        try:
            return _publish_once(
                payload,
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                label=error_label,
                producer_attestation=producer_attestation,
                independent_verifier_headers=independent_verifier_headers,
            )
        except IngestPublishError as exc:
            if not exc.retryable or attempt >= max_retries:
                raise
            exponential = min(
                MAX_RETRY_DELAY_SECONDS,
                retry_base_seconds * (2**attempt),
            )
            delay = exc.retry_after if exc.retry_after is not None else exponential
            sleep(max(0.0, min(MAX_RETRY_DELAY_SECONDS, delay)))
    raise AssertionError("unreachable")


def _row_digest(row: dict) -> str:
    return _sha256(_canonical_json(row).encode("utf-8"))


def _batch_identity(rows: list[dict], payload: dict) -> tuple[str, str, str, str]:
    record_digests = [_row_digest(row) for row in rows]
    batch_digest = _sha256("\n".join(record_digests).encode("ascii"))
    payload_digest = _sha256(_canonical_json(payload).encode("utf-8"))
    return batch_digest, payload_digest, record_digests[0], record_digests[-1]


def iter_ledger_batches(
    ledger: ObservationLedger,
    *,
    batch_size: int = MAX_OBSERVATIONS_PER_REQUEST,
    group_by_source: bool = True,
    start_offset: int = 0,
    max_rows: int | None = None,
    min_observed_at: datetime | str | None = None,
) -> Iterator[list[dict]]:
    """Stream valid ledger rows in bounded batches.

    Source-aware batching keeps D1 ``source_runs`` useful for the Sites
    health panel even though the local append-only ledger is written by many
    collectors.  Rows from the same source are grouped up to the request
    limit; disabling ``group_by_source`` retains the historical append order
    for maintenance/replay callers.  ``start_offset`` is only a safe append
    optimization: the caller must persist it after the entire scan succeeds.
    """

    if not 1 <= batch_size <= MAX_OBSERVATIONS_PER_REQUEST:
        raise ValueError(f"batch_size must be between 1 and {MAX_OBSERVATIONS_PER_REQUEST}")
    if max_rows is not None and (isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1):
        raise ValueError("max_rows must be a positive integer")
    cutoff = _normalise_observation_cutoff(min_observed_at)
    if not group_by_source:
        batch: list[dict] = []
        seen = 0
        for observation in _iter_valid_observations(ledger, start_offset=start_offset):
            if cutoff is not None and utc(observation.observed_at) < cutoff:
                continue
            batch.append(observation.as_dict())
            seen += 1
            if len(batch) == batch_size:
                yield batch
                batch = []
            if max_rows is not None and seen >= max_rows:
                break
        if batch:
            yield batch
        return

    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    seen = 0
    for observation in _iter_valid_observations(ledger, start_offset=start_offset):
        if cutoff is not None and utc(observation.observed_at) < cutoff:
            continue
        row = observation.as_dict()
        seen += 1
        source_name = str(row.get("source_name") or "").strip() or "unknown source"
        if source_name not in buckets:
            buckets[source_name] = []
            order.append(source_name)
        bucket = buckets[source_name]
        bucket.append(row)
        if len(bucket) == batch_size:
            yield bucket
            buckets[source_name] = []
        if max_rows is not None and seen >= max_rows:
            break
    for source_name in order:
        bucket = buckets.get(source_name) or []
        if bucket:
            yield bucket


def _iter_valid_observations(
    ledger: ObservationLedger,
    *,
    start_offset: int = 0,
) -> Iterator[Observation]:
    """Stream the ledger without changing the locked model module.

    ``ObservationLedger.read`` intentionally remains part of the frozen
    prospective model inventory.  The publisher therefore owns this bounded
    reader and mirrors its diagnostics contract instead of changing the
    locked module just to optimize a transport task.
    """

    if isinstance(start_offset, bool) or not isinstance(start_offset, int) or start_offset < 0:
        raise ValueError("start_offset must be a non-negative integer")
    ledger.last_read_errors = []
    if not ledger.path.exists():
        return
    try:
        size = ledger.path.stat().st_size
    except OSError:
        return
    if start_offset > size:
        start_offset = 0
    with ledger.path.open("rb") as stream:
        stream.seek(start_offset)
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("JSON root must be an object")
                observation = Observation(**value)
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                ledger.last_read_errors.append({
                    "line": line_number,
                    "bytes": len(line),
                    "error": str(exc),
                })
                continue
            yield observation


def _observation_rights_reason(observation: Observation) -> str | None:
    policy_id = _SOURCE_NAME_TO_POLICY_ID.get(observation.source_name)
    if _is_crawl4ai_observation(observation):
        crawl4ai_reason = _crawl4ai_observation_rights_reason(observation, policy_id)
        if crawl4ai_reason is not None:
            return crawl4ai_reason
    if policy_id is None:
        return f"source_rights_unknown_provider:{observation.source_name}"
    if (
        policy_id is SourceId.OPENFOOTBALL_CURRENT
        and observation.source_url not in OPENFOOTBALL_ALLOWED_URLS
    ):
        return "openfootball_source_contract_unverified"
    redistribution = decide_source_rights(policy_id, UseCase.REDISTRIBUTION)
    if not redistribution.is_allowed:
        return (
            f"source_rights_blocked:{policy_id.value}:"
            f"{UseCase.REDISTRIBUTION.value}"
        )
    if observation.enters_model:
        model_input = decide_source_rights(policy_id, UseCase.MODEL_INPUT)
        if not model_input.is_allowed:
            return (
                f"source_rights_blocked:{policy_id.value}:"
                f"{UseCase.MODEL_INPUT.value}"
            )
    return None


def _preflight_ledger_rights(
    ledger: ObservationLedger,
    *,
    start_offset: int,
    max_rows: int | None,
    min_observed_at: datetime | None,
    openfootball_observation_identities: list[str] | None = None,
) -> set[str]:
    """Scan the complete selected tail before the first remote request."""

    selected = 0
    source_policy_ids: set[str] = set()
    for observation in _iter_valid_observations(ledger, start_offset=start_offset):
        if min_observed_at is not None and utc(observation.observed_at) < min_observed_at:
            continue
        reason = _observation_rights_reason(observation)
        if reason is not None:
            raise ValueError(reason)
        policy_id = _SOURCE_NAME_TO_POLICY_ID.get(observation.source_name)
        if policy_id is None:  # kept explicit even though the reason gate covers it
            raise ValueError(
                f"source_rights_unknown_provider:{observation.source_name}"
            )
        source_policy_ids.add(policy_id.value)
        if (
            openfootball_observation_identities is not None
            and policy_id is SourceId.OPENFOOTBALL_CURRENT
        ):
            openfootball_observation_identities.append(
                _sha256(_canonical_json(observation.as_dict()).encode("utf-8"))
            )
        selected += 1
        if max_rows is not None and selected >= max_rows:
            break
    if ledger.last_read_errors:
        raise ValueError("ledger_preflight_integrity_failed")
    return source_policy_ids


class _ReceiptStore:
    """Small append-only receipt journal with inter-process locking."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = Path(f"{path}.lock")

    def _lock(self) -> Any:
        store = self

        class _Lock:
            def __init__(self) -> None:
                self.handle: Any = None

            def __enter__(self) -> "_Lock":
                store.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = store.lock_path.open("a+")
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(
                self,
                exc_type: Any,
                exc: Any,
                traceback: Any,
            ) -> Literal[False]:
                if self.handle is None:
                    return False
                try:
                    if fcntl is not None:
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    self.handle.close()
                return False

        return _Lock()

    @staticmethod
    def _receipt_key(value: Mapping[str, object], *, line_number: int) -> tuple[str, str] | None:
        if value.get("status") != "completed":
            return None
        # Older receipts did not bind the current artifact-backed maturity
        # gate. Keep them as immutable audit history, but never use them to
        # suppress a new production request.
        if value.get("schemaVersion") != PUBLICATION_RECEIPT_SCHEMA_VERSION:
            return None
        batch_digest = value.get("batchDigest")
        stream = value.get("stream")
        destinations = value.get("destinations")
        epoch = value.get("publicationEpoch")
        publication_gate_sha = value.get("publicationGateSha256")
        outbound_digest = value.get("outboundPayloadSha256")
        receipt_digest = value.get("receiptIdentitySha256")
        completed_at = value.get("completedAt")
        row_count = value.get("rowCount")
        payload_digest = value.get("payloadSha256")
        response = value.get("response")
        response_digest = value.get("responseSha256")
        if (
            not isinstance(batch_digest, str)
            or not _SHA256_RE.fullmatch(batch_digest.lower())
            or not isinstance(stream, str)
            or not isinstance(destinations, Mapping)
            or not all(isinstance(role, str) and isinstance(url, str) for role, url in destinations.items())
            or not isinstance(epoch, str)
            or not isinstance(publication_gate_sha, str)
            or not _SHA256_RE.fullmatch(publication_gate_sha.lower())
            or not isinstance(outbound_digest, str)
            or not _SHA256_RE.fullmatch(outbound_digest.lower())
            or not isinstance(receipt_digest, str)
            or not _SHA256_RE.fullmatch(receipt_digest.lower())
            or not isinstance(completed_at, str)
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
            or not isinstance(payload_digest, str)
            or not _SHA256_RE.fullmatch(payload_digest.lower())
            or payload_digest.lower() != str(outbound_digest).lower()
            or not isinstance(response, Mapping)
            or not isinstance(response_digest, str)
            or not _SHA256_RE.fullmatch(response_digest.lower())
            or response_digest.lower()
            != _sha256(_canonical_json(response).encode("utf-8"))
        ):
            raise ValueError(
                f"invalid publication receipt line {line_number}: scoped identity missing"
            )
        try:
            completed_timestamp = datetime.fromisoformat(
                completed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"invalid publication receipt line {line_number}: completedAt invalid"
            ) from exc
        if completed_timestamp.tzinfo is None:
            raise ValueError(
                f"invalid publication receipt line {line_number}: completedAt timezone missing"
            )
        scope = publication_scope(
            stream=stream,
            destinations={str(role): str(url) for role, url in destinations.items()},
            publication_epoch_value=resolve_publication_epoch(epoch, dry_run=False),
            publication_gate_sha256=publication_gate_sha,
        )
        if value.get("publicationScopeSha256") != scope["publicationScopeSha256"]:
            raise ValueError(
                f"invalid publication receipt line {line_number}: scope digest mismatch"
            )
        identity_document = {**scope, "outboundPayloadSha256": outbound_digest.lower()}
        expected = _sha256(_canonical_json(identity_document).encode("utf-8"))
        if receipt_digest.lower() != expected:
            raise ValueError(
                f"invalid publication receipt line {line_number}: identity digest mismatch"
            )
        return batch_digest.lower(), expected

    def _completed_records_unlocked(self) -> dict[tuple[str, str], dict[str, object]]:
        completed: dict[tuple[str, str], dict[str, object]] = {}
        if not self.path.exists():
            return completed
        with self.path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid publication receipt line {line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"invalid publication receipt line {line_number}: root is not an object")
                key = self._receipt_key(value, line_number=line_number)
                if key is not None:
                    completed[key] = value
        return completed

    def _completed_unlocked(self) -> set[tuple[str, str]]:
        return set(self._completed_records_unlocked())

    def completed(self) -> set[tuple[str, str]]:
        with self._lock():
            return self._completed_unlocked()

    def completed_records(self) -> dict[tuple[str, str], dict[str, object]]:
        with self._lock():
            return self._completed_records_unlocked()

    def batching_mode(self) -> str:
        """Return the receipt journal's batching contract.

        Journals written before source-aware batching have no mode marker.  A
        publisher must keep using their legacy append order or every digest
        would change and the full ledger would be replayed.  New or empty
        journals opt into source-aware batching.
        """

        if not self.path.exists():
            return "source_aware"
        modes: set[str] = set()
        with self._lock():
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid publication receipt line {line_number}: {exc}") from exc
                    if not isinstance(value, dict) or value.get("stream") != "intelligence" or value.get("status") != "completed":
                        continue
                    mode = value.get("batchingMode")
                    # Missing metadata means a pre-source-aware journal.
                    modes.add(mode if mode in {"legacy", "source_aware"} else "legacy")
        if "legacy" in modes:
            return "legacy"
        return "source_aware"

    def append_completed(self, receipt: dict) -> bool:
        """Append exactly one completed receipt unless it already exists."""

        with self._lock():
            completed = self._completed_unlocked()
            key = self._receipt_key(receipt, line_number=0)
            if key is None:
                raise ValueError(
                    "completed receipt must use the current scoped schema"
                )
            if key in completed:
                return False
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(_canonical_json(receipt) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return True


# Public alias for sibling publishers.  The journal format and locking policy
# remain owned by this module so fixture/forecast/intelligence streams resume
# through the same append-only receipt semantics.
PublicationReceiptStore = _ReceiptStore


def _default_receipts_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f"{ledger_path.name}.sites-receipts.jsonl")


def _default_snapshot_for_ledger(ledger_path: Path) -> Path:
    """Locate the sibling current snapshot without requiring deployment config."""

    return ledger_path.parent.parent / "current.json"


def _load_news_fixture_catalog(snapshot_path: Path | None) -> list[dict]:
    """Load only the validated ESPN fixture identity rows for news joins.

    Publication is allowed to degrade when the current snapshot is missing or
    malformed: raw intelligence still publishes, but no news row is guessed
    into a fixture.  The snapshot itself is written by the causal sync and is
    validated by the current-data contract before this publisher runs.
    """

    if snapshot_path is None or not snapshot_path.exists():
        return []
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(snapshot, Mapping):
        return []
    return [dict(row) for row in fixture_rows(snapshot)]


def _default_lineup_diagnostics_receipts_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(f"{snapshot_path.name}.lineup-diagnostics-receipts.jsonl")


def _default_source_health_receipts_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(f"{snapshot_path.name}.source-health-receipts.jsonl")


def _response_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-sensitive receipt fields from the API response."""

    summary: dict[str, Any] = {}
    for key in ("accepted", "skippedDuplicate", "sourceRunId"):
        value = response.get(key)
        if isinstance(value, (bool, int, float, str)) or value is None:
            summary[key] = value
    resolution = response.get("entityResolution")
    if isinstance(resolution, dict):
        bounded_resolution: dict[str, int] = {}
        for key in ("alreadyBound", "resolved", "ambiguous", "unresolved"):
            value = resolution.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                bounded_resolution[key] = value
        if bounded_resolution:
            summary["entityResolution"] = bounded_resolution
    projections = response.get("projections")
    if isinstance(projections, dict):
        bounded_projections: dict[str, Any] = {}
        for key in ("weather", "odds", "news", "availability", "outcome", "skipped"):
            value = projections.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                bounded_projections[key] = value
        skipped_by_reason = projections.get("skippedByReason")
        if isinstance(skipped_by_reason, dict):
            bounded_reasons: dict[str, int] = {}
            for key, value in skipped_by_reason.items():
                if isinstance(key, str) and len(key) <= 120 and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                    bounded_reasons[key] = value
            if bounded_reasons:
                bounded_projections["skippedByReason"] = bounded_reasons
        if bounded_projections:
            summary["projections"] = bounded_projections
    return summary


def _validate_ingest_response(response: Mapping[str, object], *, row_count: int) -> None:
    accepted = response.get("accepted")
    skipped = response.get("skippedDuplicate")
    source_run_id = response.get("sourceRunId")
    if (
        not isinstance(accepted, int)
        or isinstance(accepted, bool)
        or accepted < 0
        or not isinstance(skipped, int)
        or isinstance(skipped, bool)
        or skipped < 0
        or accepted + skipped != row_count
        or not isinstance(source_run_id, int)
        or isinstance(source_run_id, bool)
        or source_run_id <= 0
    ):
        raise ValueError("ingest response acknowledgement mismatch")
    projections = response.get("projections")
    skipped_by_reason = (
        projections.get("skippedByReason")
        if isinstance(projections, Mapping)
        else None
    )
    typed_projection_failures = (
        skipped_by_reason.get("typed_projection_write_failed")
        if isinstance(skipped_by_reason, Mapping)
        else None
    )
    if (
        isinstance(typed_projection_failures, int)
        and not isinstance(typed_projection_failures, bool)
        and typed_projection_failures > 0
    ):
        # The raw observation may already be durable, but the typed D1
        # projection is still repairable by the endpoint's idempotency key.
        # A completed publisher receipt would suppress that repair forever.
        raise ValueError("ingest typed projection acknowledgement incomplete")


def _completed_ingest_receipt_is_valid(
    receipt: Mapping[str, object],
    *,
    payload: Mapping[str, object],
    row_count: int,
) -> bool:
    response = receipt.get("response")
    if not isinstance(response, Mapping) or receipt.get("rowCount") != row_count:
        return False
    if receipt.get("payloadSha256") != _sha256(
        _canonical_json(payload).encode("utf-8")
    ):
        return False
    try:
        _validate_ingest_response(response, row_count=row_count)
    except ValueError:
        return False
    return True


def publish_ledger(
    ledger_path: Path,
    *,
    snapshot_path: Path | None = None,
    cursor_path: Path | None = None,
    endpoint: str = "",
    token: str = "",
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    group_by_source: bool | None = None,
    batch_size: int = MAX_OBSERVATIONS_PER_REQUEST,
    limit: int | None = None,
    min_observed_at: datetime | str | None = None,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> dict:
    """Stream and publish a ledger, resuming batches in the receipt journal."""

    if not ledger_path.exists():
        raise FileNotFoundError(ledger_path)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_OBSERVATIONS_PER_REQUEST:
        raise ValueError(f"batch_size must be between 1 and {MAX_OBSERVATIONS_PER_REQUEST}")
    cutoff = _normalise_observation_cutoff(min_observed_at)
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    maturity_receipt_sha = str(maturity["receiptSha256"])
    destination_scope = publication_scope(
        stream="intelligence",
        destinations={"ingest": endpoint or "https://invalid.example/api/ingest"},
        publication_epoch_value=epoch,
        publication_gate_sha256=maturity_receipt_sha,
    )
    receipt_store = None if dry_run else _ReceiptStore(receipts_path or _default_receipts_path(ledger_path))
    effective_cursor_path = cursor_path or _default_cursor_path(ledger_path)
    # A dry-run must never advance the cursor, but it may safely read the
    # last completed cursor.  This keeps operator preflights representative
    # of the production tail scan instead of rescanning hundreds of MB from
    # byte zero on every verification.  Missing or invalid cursors still
    # fail back to offset zero and retain the full-integrity diagnostics.
    cursor_state = _load_ledger_cursor(
        ledger_path,
        effective_cursor_path,
        publication_scope_sha256=str(destination_scope["publicationScopeSha256"]),
    )
    start_offset = int(cursor_state["offset"])
    fixture_catalog = _load_news_fixture_catalog(
        snapshot_path if snapshot_path is not None else _default_snapshot_for_ledger(ledger_path)
    )
    rights_ledger = ObservationLedger(ledger_path)
    openfootball_observation_identities: list[str] = []
    selected_source_policy_ids = _preflight_ledger_rights(
        rights_ledger,
        start_offset=start_offset,
        max_rows=limit,
        min_observed_at=cutoff,
        openfootball_observation_identities=openfootball_observation_identities,
    )
    admitted_openfootball_identities = set(openfootball_observation_identities)
    if (
        not dry_run
        and SourceId.OPENFOOTBALL_CURRENT.value in selected_source_policy_ids
        and openfootball_raw_archive_dir is not None
    ):
        raw_provenance_status = require_openfootball_ledger_raw_admission(
            snapshot_path,
            openfootball_raw_archive_dir=openfootball_raw_archive_dir,
            ledger_observation_identities=openfootball_observation_identities,
        )
    else:
        raw_provenance_status = require_raw_source_producer_receipt(
            selected_source_policy_ids,
            dry_run=dry_run,
        )
    batching_mode = (
        "source_aware"
        if group_by_source is not False and (group_by_source is True or receipt_store is None)
        else "legacy"
    )
    if group_by_source is None and receipt_store is not None:
        batching_mode = receipt_store.batching_mode()
    source_aware = batching_mode == "source_aware"
    completed = receipt_store.completed_records() if receipt_store else {}
    result: dict[str, Any] = {
        "accepted": 0,
        "skippedDuplicate": 0,
        "rows": 0,
        "batches": 0,
        "requests": 0,
        "receiptsSkipped": 0,
        "ledgerErrorCount": 0,
        "ledgerErrors": [],
        "ledgerScanComplete": False,
        "ledgerStartOffset": start_offset,
        "ledgerCursorUsed": bool(start_offset),
        "ledgerCursorSaved": False,
        "entityResolution": {
            "alreadyBound": 0,
            "resolved": 0,
            "ambiguous": 0,
            "unresolved": 0,
        },
        "projections": {
            "weather": 0,
            "odds": 0,
            "news": 0,
            "availability": 0,
            "outcome": 0,
            "skipped": 0,
            "skippedByReason": {},
        },
        "newsResolution": {
            "algorithm": "exact_team_pair_or_team_pre_kickoff_recent_v3",
            "model_use": "display_only",
            "candidate_fixture_count": len(fixture_catalog),
            "input_news_rows": 0,
            "linked_news_rows": 0,
            "fixture_linked_news_rows": 0,
            "team_linked_news_rows": 0,
            "unlinked_news_rows": 0,
            "unlinked_reasons": {},
        },
        "failed": 0,
        "status": "dry_run" if dry_run else "ok",
        "batchingMode": batching_mode,
        "rawProvenanceStatus": raw_provenance_status,
    }
    if cutoff is not None:
        result["minObservedAt"] = cutoff.isoformat()
    if start_offset:
        result["receiptsSkipped"] = int(cursor_state.get("completed_batches", 0))
    ledger = ObservationLedger(ledger_path)
    for batch_index, rows in enumerate(
        iter_ledger_batches(
            ledger,
            group_by_source=source_aware,
            batch_size=batch_size,
            start_offset=start_offset,
            max_rows=limit,
            min_observed_at=cutoff,
        ),
        start=0,
    ):
        result["rows"] += len(rows)
        result["batches"] += 1
        # Re-run the rights decision on the exact rows that reached this
        # batch.  The first scan is intentionally performed before raw
        # admission, but an append-only writer may add a row between scans;
        # no newly appended provider may bypass the preflight gate.
        for row in rows:
            try:
                batch_observation = Observation(**row)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("ledger_preflight_integrity_failed") from exc
            batch_reason = _observation_rights_reason(batch_observation)
            if batch_reason is not None:
                raise ValueError(batch_reason)
        resolved_rows, resolution = resolve_news_rows(rows, fixture_catalog)
        # The ledger is append-only and may be written by a concurrent poller
        # between the rights preflight and this streaming pass.  Reject any
        # newly observed OpenFootball row that was not part of the preflight
        # identity set, so a TOCTOU append cannot become the first remote
        # request's payload.
        if admitted_openfootball_identities:
            for row in resolved_rows:
                if row.get("source_name") != "OpenFootball":
                    continue
                identity = _sha256(_canonical_json(row).encode("utf-8"))
                if identity not in admitted_openfootball_identities:
                    raise ValueError("openfootball_ledger_row_unbound")
        aggregate_resolution = result["newsResolution"]
        if isinstance(aggregate_resolution, dict):
            for key in ("input_news_rows", "linked_news_rows", "fixture_linked_news_rows", "team_linked_news_rows", "unlinked_news_rows"):
                previous = aggregate_resolution.get(key, 0)
                current = resolution.get(key, 0)
                aggregate_resolution[key] = (
                    (previous if isinstance(previous, int) else 0)
                    + (current if isinstance(current, int) else 0)
                )
            reasons = aggregate_resolution.get("unlinked_reasons")
            unresolved_reasons = resolution.get("unlinked_reasons")
            if isinstance(reasons, dict) and isinstance(unresolved_reasons, Mapping):
                for reason, count in unresolved_reasons.items():
                    if isinstance(reason, str) and isinstance(count, int):
                        reasons[reason] = int(reasons.get(reason, 0)) + count
        metadata = {"newsResolution": resolution} if resolution.get("input_news_rows", 0) else None
        payload = build_ingest_payload(resolved_rows, metadata=metadata)
        batch_digest, payload_digest, first_digest, last_digest = _batch_identity(resolved_rows, payload)
        receipt_identity = publication_receipt_identity(
            stream="intelligence",
            destinations={
                "ingest": endpoint or "https://invalid.example/api/ingest"
            },
            publication_epoch_value=epoch,
            publication_gate_sha256=maturity_receipt_sha,
            outbound_payload=payload,
        )
        receipt_key = (batch_digest, str(receipt_identity["receiptIdentitySha256"]))
        completed_receipt = completed.get(receipt_key)
        if completed_receipt is not None:
            if not _completed_ingest_receipt_is_valid(
                completed_receipt,
                payload=payload,
                row_count=len(resolved_rows),
            ):
                raise ValueError("intelligence_completed_receipt_invalid")
            result["receiptsSkipped"] += 1
            continue
        if dry_run:
            continue
        batch_source_ids: set[str] = set()
        for row in resolved_rows:
            source_name = row.get("source_name")
            policy_id = (
                _SOURCE_NAME_TO_POLICY_ID.get(source_name)
                if isinstance(source_name, str)
                else None
            )
            if policy_id is None:
                raise ValueError(f"source_rights_unknown_provider:{source_name}")
            batch_source_ids.add(policy_id.value)
        producer_attestation = build_producer_attestation_context(
            maturity_context=maturity_context,
            maturity_verification=maturity,
            stream="intelligence_ingest",
            rights_use_case="redistribution",
            source_policy_ids=batch_source_ids,
            publication_epoch=epoch,
            dry_run=False,
        )
        result["requests"] += 1
        try:
            response = publish_payload(
                payload,
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep=sleep,
                producer_attestation=producer_attestation,
            )
            if not isinstance(response, dict):
                raise ValueError("ingest response must be a JSON object")
            _validate_ingest_response(response, row_count=len(resolved_rows))
            result["accepted"] += int(response.get("accepted", 0) or 0)
            result["skippedDuplicate"] += int(response.get("skippedDuplicate", 0) or 0)
            server_resolution = response.get("entityResolution")
            if isinstance(server_resolution, dict):
                aggregate = result["entityResolution"]
                if isinstance(aggregate, dict):
                    for key in aggregate:
                        value = server_resolution.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            aggregate[key] += value
            projections = response.get("projections")
            if isinstance(projections, dict):
                aggregate_projections = result["projections"]
                if isinstance(aggregate_projections, dict):
                    for key in ("weather", "odds", "news", "availability", "outcome", "skipped"):
                        value = projections.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                            aggregate_projections[key] += value
                    skipped_by_reason = projections.get("skippedByReason")
                    if isinstance(skipped_by_reason, dict):
                        reasons = aggregate_projections["skippedByReason"]
                        if isinstance(reasons, dict):
                            for reason, value in skipped_by_reason.items():
                                if isinstance(reason, str) and len(reason) <= 120 and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                                    reasons[reason] = reasons.get(reason, 0) + value
            response_summary = _response_summary(response)
            receipt = {
                **receipt_identity,
                "batchIndex": batch_index,
                "firstRecordDigest": first_digest,
                "lastRecordDigest": last_digest,
                "rowCount": len(resolved_rows),
                "batchDigest": batch_digest,
                "payloadSha256": payload_digest,
                "batchingMode": batching_mode,
                "completedAt": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "response": response_summary,
                "responseSha256": _sha256(
                    _canonical_json(response_summary).encode("utf-8")
                ),
            }
            if receipt_store is None:
                raise AssertionError("receipt store is unavailable outside dry-run")
            receipt_store.append_completed(receipt)
            completed[receipt_key] = receipt
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result["failed"] = 1
            result["status"] = "failed"
            result["failedBatch"] = batch_index
            result["error"] = _redact(exc, token)
            break

    else:
        # The generator was exhausted, so ``last_read_errors`` represents the
        # complete ledger rather than only the prefix reached before a failed
        # network call.
        # A caller-provided limit is deliberately a bounded prefix, not proof
        # that the entire append-only ledger was scanned.  Never advance the
        # resumable cursor from a partial scan.
        result["ledgerScanComplete"] = limit is None
        if not dry_run and limit is None:
            try:
                _save_ledger_cursor(
                    ledger_path,
                    effective_cursor_path,
                    ledger_path.stat().st_size,
                    completed_batches=int(cursor_state.get("completed_batches", 0)) + int(result["batches"]),
                    publication_scope_sha256=str(
                        destination_scope["publicationScopeSha256"]
                    ),
                )
                result["ledgerCursorSaved"] = True
            except (OSError, ValueError) as exc:
                result["ledgerCursorError"] = _redact(exc, token)
    errors = list(ledger.last_read_errors)
    result["ledgerErrorCount"] = len(errors)
    result["ledgerErrors"] = errors[:10]
    if errors and result["status"] == "ok":
        result["status"] = "partial_integrity"
    elif errors and result["status"] == "dry_run":
        result["status"] = "dry_run_partial_integrity"
    return result


def publish_lineup_diagnostics(
    snapshot_path: Path,
    *,
    endpoint: str = "",
    token: str = "",
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Publish the current lineup-poll coverage as an audit-only source run.

    The snapshot diagnostic is deliberately carried in ``sourceRun.metadata``
    with no observations.  That keeps polling failures visible to researchers
    without allowing a missing or blocked source to become a model feature.
    One snapshot has one deterministic receipt, so retries are idempotent.
    """

    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a JSON object")
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    payload = build_lineup_diagnostics_payload(snapshot)
    source_run = payload["sourceRun"]
    metadata_digest = _sha256(_canonical_json(source_run.get("metadata", {})).encode("utf-8"))
    payload_digest = _sha256(_canonical_json(payload).encode("utf-8"))
    receipt_identity = publication_receipt_identity(
        stream="lineup_diagnostics",
        destinations={"ingest": endpoint or "https://invalid.example/api/ingest"},
        publication_epoch_value=epoch,
        publication_gate_sha256=str(maturity["receiptSha256"]),
        outbound_payload=payload,
    )
    receipt_key = (metadata_digest, str(receipt_identity["receiptIdentitySha256"]))
    result: dict = {
        "status": "dry_run" if dry_run else "ok",
        "requests": 0,
        "receiptsSkipped": 0,
        "accepted": 0,
        "skippedDuplicate": 0,
        "sourceRun": {
            "provider": source_run["provider"],
            "sourceType": source_run["sourceType"],
            "status": source_run["status"],
            "recordsSeen": source_run["recordsSeen"],
            "asOf": source_run.get("metadata", {}).get("asOf"),
        },
    }
    if dry_run:
        return result

    producer_attestation = build_producer_attestation_context(
        maturity_context=maturity_context,
        maturity_verification=maturity,
        stream="audit_only",
        rights_use_case="audit_only",
        source_policy_ids={SourceId.OFFICIAL_LEAGUE_LINEUPS.value},
        publication_epoch=epoch,
        dry_run=False,
    )

    receipt_store = PublicationReceiptStore(
        receipts_path or _default_lineup_diagnostics_receipts_path(snapshot_path)
    )
    completed = receipt_store.completed_records()
    completed_receipt = completed.get(receipt_key)
    if completed_receipt is not None:
        if not _completed_ingest_receipt_is_valid(
            completed_receipt,
            payload=payload,
            row_count=0,
        ):
            raise ValueError("lineup_completed_receipt_invalid")
        result["receiptsSkipped"] = 1
        result["status"] = "already_published"
        return result

    result["requests"] = 1
    try:
        response = publish_payload(
            payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            sleep=sleep,
            error_label="lineup diagnostics ingest",
            producer_attestation=producer_attestation,
        )
        if not isinstance(response, dict):
            raise ValueError("ingest response must be a JSON object")
        _validate_ingest_response(response, row_count=0)
        result["accepted"] = int(response.get("accepted", 0) or 0)
        result["skippedDuplicate"] = int(response.get("skippedDuplicate", 0) or 0)
        response_summary = _response_summary(response)
        receipt = {
            **receipt_identity,
            "batchIndex": 0,
            "firstRecordDigest": metadata_digest,
            "lastRecordDigest": metadata_digest,
            "rowCount": 0,
            "batchDigest": metadata_digest,
            "payloadSha256": payload_digest,
            "batchingMode": "single_snapshot",
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "response": response_summary,
            "responseSha256": _sha256(
                _canonical_json(response_summary).encode("utf-8")
            ),
        }
        receipt_store.append_completed(receipt)
        result["response"] = _response_summary(response)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        result["status"] = "failed"
        result["failed"] = 1
        result["error"] = _redact(exc, token)
    return result


def publish_source_health_diagnostics(
    snapshot_path: Path,
    *,
    endpoint: str = "",
    token: str = "",
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    prospective_gate_metadata: Mapping[str, object] | None = None,
) -> dict:
    """Publish per-source registry health as isolated audit-only runs."""

    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a JSON object")
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    payloads = build_source_health_payloads(
        snapshot,
        prospective_gate_metadata=prospective_gate_metadata,
    )
    result: dict = {
        "status": "dry_run" if dry_run else "ok",
        "sources": len(payloads),
        "requests": 0,
        "published": 0,
        "receiptsSkipped": 0,
        "failed": 0,
        "sourceStatuses": [],
    }
    if dry_run:
        result["sourceStatuses"] = [
            {
                "provider": payload["sourceRun"]["provider"],
                "status": payload["sourceRun"]["status"],
                "recordsSeen": payload["sourceRun"]["recordsSeen"],
                "errorCode": payload["sourceRun"].get("errorCode"),
            }
            for payload in payloads
        ]
        return result

    receipt_store = PublicationReceiptStore(
        receipts_path or _default_source_health_receipts_path(snapshot_path)
    )
    completed = receipt_store.completed_records()
    for index, payload in enumerate(payloads):
        source_run = payload["sourceRun"]
        metadata = source_run.get("metadata", {})
        metadata_digest = _sha256(_canonical_json(metadata).encode("utf-8"))
        payload_digest = _sha256(_canonical_json(payload).encode("utf-8"))
        receipt_identity = publication_receipt_identity(
            stream="source_health_diagnostics",
            destinations={"ingest": endpoint},
            publication_epoch_value=epoch,
            publication_gate_sha256=str(maturity["receiptSha256"]),
            outbound_payload=payload,
        )
        receipt_key = (
            metadata_digest,
            str(receipt_identity["receiptIdentitySha256"]),
        )
        provider = str(source_run.get("provider") or "unknown")
        summary = {
            "provider": provider,
            "status": source_run.get("status"),
            "recordsSeen": source_run.get("recordsSeen", 0),
            "errorCode": source_run.get("errorCode"),
        }
        completed_receipt = completed.get(receipt_key)
        if completed_receipt is not None:
            if not _completed_ingest_receipt_is_valid(
                completed_receipt,
                payload=payload,
                row_count=0,
            ):
                raise ValueError("source_health_completed_receipt_invalid")
            result["receiptsSkipped"] += 1
            result["sourceStatuses"].append({**summary, "receipt": "skipped"})
            continue
        result["requests"] += 1
        try:
            source_id = (
                metadata.get("sourceId")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("source health policy identity is missing")
            producer_attestation = build_producer_attestation_context(
                maturity_context=maturity_context,
                maturity_verification=maturity,
                stream="audit_only",
                rights_use_case="audit_only",
                source_policy_ids={source_id},
                publication_epoch=epoch,
                dry_run=False,
            )
            response = publish_payload(
                payload,
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep=sleep,
                error_label=f"source health ({provider})",
                producer_attestation=producer_attestation,
            )
            if not isinstance(response, dict):
                raise ValueError("ingest response must be a JSON object")
            _validate_ingest_response(response, row_count=0)
            response_summary = _response_summary(response)
            receipt = {
                **receipt_identity,
                "batchIndex": index,
                "sourceId": metadata.get("sourceId") if isinstance(metadata, Mapping) else None,
                "provider": provider,
                "rowCount": 0,
                "batchDigest": metadata_digest,
                "payloadSha256": payload_digest,
                "batchingMode": "one_source_snapshot",
                "completedAt": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "response": response_summary,
                "responseSha256": _sha256(
                    _canonical_json(response_summary).encode("utf-8")
                ),
            }
            receipt_store.append_completed(receipt)
            completed[receipt_key] = receipt
            result["published"] += 1
            result["sourceStatuses"].append({**summary, "receipt": "published", "response": _response_summary(response)})
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result["failed"] += 1
            result["sourceStatuses"].append({**summary, "receipt": "failed", "error": _redact(exc, token)})
            # A single provider's D1/transport failure must not hide the
            # status of the remaining sources in this audit stream.
            continue
    if result["failed"]:
        result["status"] = "partial" if result["published"] or result["receiptsSkipped"] else "failed"
    elif result["published"] == 0 and result["receiptsSkipped"]:
        result["status"] = "already_published"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root_default = (
        Path(os.environ["MATCHLINE_RUNTIME_DIR"])
        if os.environ.get("MATCHLINE_RUNTIME_DIR")
        else None
    )
    parser.add_argument("ledger", type=Path)
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="current snapshot used for strict news-to-fixture joins (default: sibling data/live/current.json)",
    )
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        help="durable OpenFootball raw archive used to verify publication rows",
    )
    parser.add_argument(
        "--cursor",
        type=Path,
        help="append cursor sidecar (default: ledger.sites-cursor.json)",
    )
    parser.add_argument(
        "--receipts",
        type=Path,
        help="append-only receipt JSONL (default: MATCHLINE_INGEST_RECEIPTS or ledger sidecar)",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan batches without network or receipts")
    parser.add_argument(
        "--publication-epoch",
        default=os.environ.get("MATCHLINE_PUBLICATION_EPOCH"),
        help="operator-controlled D1 deployment epoch required for writes",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--retry-base-seconds", type=float, default=DEFAULT_RETRY_BASE_SECONDS)
    parser.add_argument(
        "--maturity-lock",
        type=Path,
        default=Path("docs/evidence/prospective-model-lock-current.json"),
    )
    parser.add_argument(
        "--maturity-cycle",
        type=Path,
        default=Path("docs/evidence/prospective-cycle-latest.json"),
    )
    parser.add_argument("--maturity-receipt", type=Path)
    parser.add_argument("--maturity-runtime-root", type=Path, default=runtime_root_default)
    parser.add_argument("--maturity-platform-verification", type=Path)
    parser.add_argument("--maturity-strict-report", type=Path)
    parser.add_argument("--maturity-evaluation", type=Path)
    parser.add_argument("--maturity-offline-snapshot", type=Path)
    parser.add_argument("--maturity-sites-build-evidence", type=Path)
    parser.add_argument("--maturity-test-evidence", type=Path)
    parser.add_argument("--maturity-publication-audit", type=Path)
    parser.add_argument("--maturity-prediction-archive", type=Path)
    parser.add_argument("--maturity-repository-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    endpoint = os.environ.get("MATCHLINE_INGEST_URL", "")
    token = os.environ.get("MATCHLINE_INGEST_TOKEN", "")
    receipts_path = args.receipts or (
        Path(os.environ["MATCHLINE_INGEST_RECEIPTS"])
        if os.environ.get("MATCHLINE_INGEST_RECEIPTS")
        else None
    )
    try:
        maturity_context = None
        effective_snapshot = args.snapshot or _default_snapshot_for_ledger(args.ledger)
        if (
            args.maturity_receipt is not None
            and args.maturity_runtime_root is not None
            and args.maturity_platform_verification is not None
            and args.maturity_strict_report is not None
            and args.maturity_evaluation is not None
            and args.maturity_offline_snapshot is not None
            and args.maturity_sites_build_evidence is not None
            and args.maturity_test_evidence is not None
            and args.maturity_publication_audit is not None
            and args.maturity_prediction_archive is not None
        ):
            maturity_context = build_publication_maturity_context(
                receipt_path=args.maturity_receipt,
                platform_verification_path=args.maturity_platform_verification,
                strict_report_path=args.maturity_strict_report,
                prospective_lock_path=args.maturity_lock,
                cycle_path=args.maturity_cycle,
                evaluation_path=args.maturity_evaluation,
                live_snapshot_path=effective_snapshot,
                offline_snapshot_path=args.maturity_offline_snapshot,
                sites_build_evidence_path=args.maturity_sites_build_evidence,
                test_evidence_path=args.maturity_test_evidence,
                publication_audit_path=args.maturity_publication_audit,
                prediction_archive_path=args.maturity_prediction_archive,
                publication_diagnostic_path=effective_snapshot.with_name(
                    "current.publication.json"
                ),
                runtime_root=args.maturity_runtime_root,
                repository_root=args.maturity_repository_root,
            )
        elif not args.dry_run:
            raise ValueError("complete maturity artifact paths are required")
        result = publish_ledger(
            args.ledger,
            snapshot_path=args.snapshot,
            cursor_path=args.cursor,
            endpoint=endpoint,
            token=token,
            receipts_path=receipts_path,
            publication_epoch=args.publication_epoch,
            maturity_context=maturity_context,
            dry_run=args.dry_run,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            openfootball_raw_archive_dir=args.openfootball_raw_archive_dir,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"publish failed: {_redact(exc, token)}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 1 if result["failed"] or result["ledgerErrorCount"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
