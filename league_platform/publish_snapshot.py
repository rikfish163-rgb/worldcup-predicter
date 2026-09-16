"""Publish commercially reusable current fixture identities to Sites D1.

This publisher is deliberately separate from the prospective model lock.  It
uses only the current schedule snapshot, strips result fields before building
the wire payload, preserves the fact source independently from entity aliases,
and keeps an append-only receipt journal so a partially published run can
resume without replaying completed batches.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unicodedata
from typing import Any
from urllib.parse import urlparse

from league_platform.fixture_feed import fixture_rows
from league_platform.fixture_serving import (
    fixture_commercial_reuse_status,
    require_fixture_serving,
)
from league_platform.identity import canonical_team_name, team_id
from league_platform.publish_intelligence import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_SECONDS,
    OPENFOOTBALL_RAW_PROVENANCE_CANDIDATE,
    PublicationReceiptStore,
    PublicationMaturityContext,
    build_producer_attestation_context,
    build_publication_maturity_context,
    publication_receipt_identity,
    require_publication_maturity,
    resolve_publication_epoch,
    publish_payload,
)
from league_platform.source_rights import SourceId, UseCase
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    verify_openfootball_snapshot_admission,
)


MAX_FIXTURES_PER_REQUEST = 100
MAX_PAYLOAD_BYTES = 512 * 1024
COMPETITIONS: dict[str, tuple[str, str]] = {
    "csl": ("中超", "中国"),
    "premier-league": ("英超", "England"),
    "championship": ("英冠", "England"),
    "la-liga": ("西甲", "Spain"),
    "bundesliga": ("德甲", "Germany"),
    "serie-a": ("意甲", "Italy"),
    "ligue-1": ("法甲", "France"),
    "five-hundred-league": ("500联赛", "China"),
}
FIXTURE_STATUSES = {"scheduled", "upcoming", "live", "finished", "postponed", "cancelled"}
MATCHLINE_ALIAS_PROVIDER = "Matchline canonical alias"
NATIVE_IDENTITY_KIND = "native"
SYNTHETIC_IDENTITY_KIND = "synthetic_alias"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _string(value: object, name: str, *, max_length: int = 240) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{name} must be a non-empty string of at most {max_length} characters")
    return value


def _source(fixture: Mapping[str, object], snapshot_as_of: str) -> tuple[str, str, str, str]:
    raw = fixture.get("source")
    if not isinstance(raw, Mapping):
        raise ValueError("fixture source provenance is required")
    provider = _string(raw.get("name"), "source.name", max_length=120)
    source_url = _string(raw.get("url"), "source.url", max_length=2048)
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("source.url must be a credential-free HTTPS URL")
    raw_hash = _string(raw.get("raw_sha256"), "source.raw_sha256", max_length=64).lower()
    if len(raw_hash) != 64 or any(character not in "0123456789abcdef" for character in raw_hash):
        raise ValueError("source.raw_sha256 must be a SHA-256 hex digest")
    observed_at = _iso(raw.get("retrieved_at") or snapshot_as_of, "source.retrieved_at")
    return provider, source_url, raw_hash, observed_at


def fixture_publication_identity(
    fixture: Mapping[str, object],
    *,
    competition_id: str | None = None,
    legacy_espn_authorized: bool = False,
    use_case: UseCase = UseCase.SERVE_CURRENT,
) -> dict[str, str]:
    """Resolve native identities or an explicitly namespaced platform alias.

    OpenFootball does not publish native fixture or team identifiers.  Its
    deterministic row ID and the platform's competition-scoped team aliases
    are therefore useful only when labelled as synthetic platform aliases.
    Other providers must supply a native fixture ID and both native team IDs;
    a canonical row ID is never silently promoted to a provider-native ID.
    """

    raw_source = fixture.get("source")
    if not isinstance(raw_source, Mapping):
        raise ValueError("fixture source provenance is required")
    fact_provider = _string(raw_source.get("name"), "source.name", max_length=120)
    rights = fixture_commercial_reuse_status(
        fixture,
        legacy_espn_authorized=legacy_espn_authorized,
        use_case=use_case,
    )
    if rights["status"] != "ok":
        raise ValueError(str(rights["reason"]))
    if fact_provider == "OpenFootball":
        resolved_competition = _string(
            competition_id or fixture.get("competition_id"),
            "fixture.competition_id",
            max_length=80,
        )
        fixture_alias = fixture.get("id")
        source_id = raw_source.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("openfootball_source_contract_unverified")
        if (
            not isinstance(fixture_alias, str)
            or not fixture_alias.startswith("openfootball:")
        ):
            raise ValueError("fixture_identity_unavailable")
        home_name = _string(fixture.get("home_team"), "home_team", max_length=160)
        away_name = _string(fixture.get("away_team"), "away_team", max_length=160)
        normalized_home = " ".join(
            unicodedata.normalize("NFKC", home_name).casefold().split()
        )
        normalized_away = " ".join(
            unicodedata.normalize("NFKC", away_name).casefold().split()
        )
        expected_fixture_alias = (
            f"openfootball:{resolved_competition}:"
            + hashlib.sha256(
                "|".join(
                    (
                        source_id,
                        str(fixture.get("season") or ""),
                        normalized_home,
                        normalized_away,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        if fixture_alias != expected_fixture_alias:
            raise ValueError("openfootball_fixture_identity_source_mismatch")
        home_canonical_name = canonical_team_name(resolved_competition, home_name)
        away_canonical_name = canonical_team_name(resolved_competition, away_name)
        return {
            "fact_provider": fact_provider,
            "provider": MATCHLINE_ALIAS_PROVIDER,
            "identity_kind": SYNTHETIC_IDENTITY_KIND,
            "fixture_id": _string(fixture_alias, "fixture.id", max_length=160),
            "home_team_id": _string(
                team_id(resolved_competition, home_name),
                "home_team_id",
                max_length=160,
            ),
            "away_team_id": _string(
                team_id(resolved_competition, away_name),
                "away_team_id",
                max_length=160,
            ),
            "home_canonical_name": _string(
                home_canonical_name,
                "home_canonical_name",
                max_length=160,
            ),
            "away_canonical_name": _string(
                away_canonical_name,
                "away_canonical_name",
                max_length=160,
            ),
        }

    native_fixture_id = raw_source.get("native_fixture_id") or fixture.get("native_fixture_id")
    if native_fixture_id in (None, ""):
        raise ValueError("fixture_identity_unavailable")
    home_provider_id = fixture.get("home_provider_team_id")
    away_provider_id = fixture.get("away_provider_team_id")
    if home_provider_id in (None, "") or away_provider_id in (None, ""):
        raise ValueError("team_identity_unavailable")
    native_fixture_id = _string(str(native_fixture_id), "fixture.native_fixture_id", max_length=160)
    home_provider_id = _string(str(home_provider_id), "home_provider_team_id", max_length=160)
    away_provider_id = _string(str(away_provider_id), "away_provider_team_id", max_length=160)
    if home_provider_id == away_provider_id:
        raise ValueError("home and away provider identities must differ")
    return {
        "fact_provider": fact_provider,
        "provider": fact_provider,
        "identity_kind": NATIVE_IDENTITY_KIND,
        "fixture_id": native_fixture_id,
        "home_team_id": home_provider_id,
        "away_team_id": away_provider_id,
    }


def build_fixture_registration_item(
    fixture: Mapping[str, object],
    *,
    snapshot_as_of: str,
    legacy_espn_authorized: bool = False,
    use_case: UseCase = UseCase.SERVE_CURRENT,
) -> dict[str, object]:
    """Convert one local snapshot row into a score-free registration item."""

    competition_id = _string(fixture.get("competition_id"), "fixture.competition_id", max_length=80)
    competition_name, competition_country = COMPETITIONS.get(competition_id, (competition_id, "unknown"))
    season = _string(fixture.get("season"), "fixture.season", max_length=40)
    kickoff_at = _iso(fixture.get("kickoff_at"), "fixture.kickoff_at")
    status = _string(fixture.get("status") or "scheduled", "fixture.status", max_length=30)
    if status not in FIXTURE_STATUSES:
        raise ValueError(f"fixture.status is invalid: {status}")
    source_provider, source_url, raw_hash, observed_at = _source(fixture, snapshot_as_of)
    identity = fixture_publication_identity(
        fixture,
        competition_id=competition_id,
        legacy_espn_authorized=legacy_espn_authorized,
        use_case=use_case,
    )
    if identity["fact_provider"] != source_provider:
        raise ValueError("fixture source identity is inconsistent")
    home_name = _string(
        identity.get("home_canonical_name") or fixture.get("home_team"),
        "home_team",
        max_length=160,
    )
    away_name = _string(
        identity.get("away_canonical_name") or fixture.get("away_team"),
        "away_team",
        max_length=160,
    )
    venue_value = fixture.get("venue")
    venue = None
    if isinstance(venue_value, Mapping):
        if venue_value.get("name") is not None:
            venue = _string(venue_value.get("name"), "venue.name", max_length=240)
    elif venue_value is not None:
        venue = _string(venue_value, "venue", max_length=240)
    return {
        "observedAt": observed_at,
        "competition": {
            "code": competition_id,
            "name": competition_name,
            "country": competition_country,
            "season": season,
        },
        "homeTeam": {
            "canonicalName": home_name,
            "country": competition_country,
            "provider": identity["provider"],
            "providerId": identity["home_team_id"],
            "identityKind": identity["identity_kind"],
            "confidence": 1.0,
        },
        "awayTeam": {
            "canonicalName": away_name,
            "country": competition_country,
            "provider": identity["provider"],
            "providerId": identity["away_team_id"],
            "identityKind": identity["identity_kind"],
            "confidence": 1.0,
        },
        "fixture": {
            "provider": identity["provider"],
            "providerFixtureId": identity["fixture_id"],
            "identityKind": identity["identity_kind"],
            "kickoffAt": kickoff_at,
            "venue": venue,
            "status": status,
        },
        "source": {
            "name": source_provider,
            "url": source_url,
            "tier": (
                OPENFOOTBALL_RAW_PROVENANCE_CANDIDATE
                if source_provider == "OpenFootball"
                else "reliable_public_provider"
            ),
            "rawHash": raw_hash,
        },
    }


def build_fixture_batch_payload(
    items: Sequence[Mapping[str, object]],
    *,
    records_seen: int | None = None,
) -> dict[str, Any]:
    """Build one bounded wire payload from score-free registration items."""

    if not items or len(items) > MAX_FIXTURES_PER_REQUEST:
        raise ValueError(f"items must contain 1-{MAX_FIXTURES_PER_REQUEST} fixtures")
    normalized = [dict(item) for item in items]
    source_providers: set[str] = set()
    for item in normalized:
        source = item.get("source")
        if isinstance(source, Mapping):
            source_providers.add(str(source.get("name")))
    if len(source_providers) != 1 or "None" in source_providers:
        raise ValueError("all fixtures in a batch must use one fact source")
    observations = [_iso(item.get("observedAt"), "observedAt") for item in normalized]
    started_at = min(observations)
    finished_at = max(observations)
    payload: dict[str, Any] = {
        "sourceRun": {
            "provider": next(iter(source_providers)),
            "sourceType": "scoreboard_snapshot",
            "startedAt": started_at,
            "finishedAt": finished_at,
            "status": "ok",
            "recordsSeen": int(records_seen if records_seen is not None else len(normalized)),
        },
        "fixtures": normalized,
    }
    body = _canonical_json(payload).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("fixture registration payload exceeds 512 KiB")
    return payload


def _snapshot_fixtures(snapshot: Mapping[str, object]) -> list[dict[str, Any]]:
    fixtures = fixture_rows(snapshot)
    if not fixtures:
        raise ValueError("current snapshot has no canonical fixture feed rows")
    return fixtures


def require_openfootball_raw_producer_receipt(
    snapshot: Mapping[str, object],
    *,
    dry_run: bool,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> str:
    """Require independent raw replay before an OpenFootball write.

    Dry-run remains a structural inspection lane. A real write must provide a
    durable archive and pass the same parser/lineage replay used by
    ``sync_live``; source ID, URL, declared hash, and deterministic alias alone
    are never treated as a producer receipt.
    """

    has_openfootball = any(
        isinstance(row.get("source"), Mapping)
        and row["source"].get("name") == "OpenFootball"
        for row in _snapshot_fixtures(snapshot)
    )
    if not has_openfootball:
        return "not_applicable"
    if dry_run:
        return OPENFOOTBALL_RAW_PROVENANCE_CANDIDATE
    if openfootball_raw_archive_dir is None:
        raise ValueError("raw_producer_receipt_required")
    try:
        admission = verify_openfootball_snapshot_admission(
            snapshot,
            archive_root=openfootball_raw_archive_dir,
        )
    except (OpenFootballRawArchiveError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"raw_producer_receipt_invalid:{exc}") from exc
    return f"verified_current_raw:{admission['admission_sha256']}"


def iter_fixture_batches(
    snapshot: Mapping[str, object],
    *,
    batch_size: int = MAX_FIXTURES_PER_REQUEST,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    if not 1 <= batch_size <= MAX_FIXTURES_PER_REQUEST:
        raise ValueError(f"batch_size must be between 1 and {MAX_FIXTURES_PER_REQUEST}")
    serving_decision = require_fixture_serving(
        snapshot,
        use_case=UseCase.REDISTRIBUTION,
    )
    legacy_espn_authorized = (
        serving_decision.get("reason")
        == "legacy_espn_serving_explicitly_authorized"
    )
    snapshot_as_of = _iso(snapshot.get("as_of"), "snapshot.as_of")
    raw_fixtures = _snapshot_fixtures(snapshot)
    selected = raw_fixtures[:limit] if limit is not None else raw_fixtures
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    items_by_source: dict[str, list[dict[str, object]]] = {}
    for row in selected:
        item = build_fixture_registration_item(
            row,
            snapshot_as_of=snapshot_as_of,
            legacy_espn_authorized=legacy_espn_authorized,
            use_case=UseCase.REDISTRIBUTION,
        )
        source = item.get("source")
        if not isinstance(source, Mapping):
            raise ValueError("fixture source provenance is required")
        source_name = _string(source.get("name"), "source.name", max_length=120)
        items_by_source.setdefault(source_name, []).append(item)
    for items in items_by_source.values():
        total = len(items)
        for start in range(0, total, batch_size):
            yield build_fixture_batch_payload(items[start:start + batch_size], records_seen=total)


def _batch_identity(payload: Mapping[str, Any]) -> tuple[str, str]:
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("payload fixtures must be a list")
    row_digest = [_sha256(_canonical_json(item).encode("utf-8")) for item in fixtures]
    batch_digest = _sha256("\n".join(row_digest).encode("ascii"))
    payload_digest = _sha256(_canonical_json(payload).encode("utf-8"))
    return batch_digest, payload_digest


def _response_summary(response: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "status", "sourceRunId", "accepted", "created", "updated", "skipped", "aliasesCreated",
        "observationsAccepted", "observationsSkippedDuplicate",
    )
    return {key: response[key] for key in keys if key in response and isinstance(response[key], (str, int, float, bool))}


def _validate_fixture_response(response: Mapping[str, object], *, row_count: int) -> None:
    accepted = response.get("accepted")
    observations_accepted = response.get("observationsAccepted")
    observations_duplicate = response.get("observationsSkippedDuplicate")
    if (
        response.get("status") != "ok"
        or not isinstance(accepted, int)
        or isinstance(accepted, bool)
        or accepted != row_count
        or not isinstance(observations_accepted, int)
        or isinstance(observations_accepted, bool)
        or not isinstance(observations_duplicate, int)
        or isinstance(observations_duplicate, bool)
        or observations_accepted + observations_duplicate != row_count
    ):
        raise ValueError("fixture response acknowledgement mismatch")


def _completed_fixture_receipt_is_valid(
    receipt: Mapping[str, object],
    *,
    payload: Mapping[str, Any],
) -> bool:
    fixtures = payload.get("fixtures")
    response = receipt.get("response")
    if not isinstance(fixtures, list) or not isinstance(response, Mapping):
        return False
    if receipt.get("rowCount") != len(fixtures):
        return False
    try:
        _validate_fixture_response(response, row_count=len(fixtures))
    except ValueError:
        return False
    return receipt.get("payloadSha256") == _sha256(
        _canonical_json(payload).encode("utf-8")
    )


def publish_snapshot(
    snapshot_path: Path,
    *,
    endpoint: str = "",
    token: str = "",
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    batch_size: int = MAX_FIXTURES_PER_REQUEST,
    limit: int | None = None,
    timeout: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> dict[str, Any]:
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot is not valid JSON: {exc}") from exc
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot root must be an object")
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    batches = list(iter_fixture_batches(snapshot, batch_size=batch_size, limit=limit))
    if openfootball_raw_archive_dir is None:
        raw_provenance_status = require_openfootball_raw_producer_receipt(
            snapshot,
            dry_run=dry_run,
        )
    else:
        raw_provenance_status = require_openfootball_raw_producer_receipt(
            snapshot,
            dry_run=dry_run,
            openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        )
    producer_attestation = build_producer_attestation_context(
        maturity_context=maturity_context,
        maturity_verification=maturity,
        stream="fixture_registration",
        rights_use_case="redistribution",
        source_policy_ids={SourceId.OPENFOOTBALL_CURRENT.value},
        publication_epoch=epoch,
        dry_run=dry_run,
    ) if batches else None
    receipt_store = None if dry_run else PublicationReceiptStore(receipts_path or snapshot_path.with_name(f"{snapshot_path.name}.fixture-receipts.jsonl"))
    completed = receipt_store.completed_records() if receipt_store else {}
    result: dict[str, Any] = {
        "fixtures": sum(len(batch["fixtures"]) for batch in batches),
        "batches": len(batches),
        "requests": 0,
        "receiptsSkipped": 0,
        "accepted": 0,
        "created": 0,
        "updated": 0,
        "observationsAccepted": 0,
        "observationsSkippedDuplicate": 0,
        "failed": 0,
        "errors": [],
        "status": "dry_run" if dry_run else "ok",
        "rawProvenanceStatus": raw_provenance_status,
    }
    for index, payload in enumerate(batches):
        batch_digest, payload_digest = _batch_identity(payload)
        receipt_identity = publication_receipt_identity(
            stream="fixtures",
            destinations={
                "register": endpoint or "https://invalid.example/api/fixtures/register"
            },
            publication_epoch_value=epoch,
            publication_gate_sha256=str(maturity["receiptSha256"]),
            outbound_payload=payload,
        )
        receipt_key = (
            batch_digest,
            str(receipt_identity["receiptIdentitySha256"]),
        )
        completed_receipt = completed.get(receipt_key)
        if completed_receipt is not None:
            if not _completed_fixture_receipt_is_valid(
                completed_receipt,
                payload=payload,
            ):
                raise ValueError("fixture_completed_receipt_invalid")
            result["receiptsSkipped"] = int(result["receiptsSkipped"]) + 1
            continue
        if dry_run:
            continue
        result["requests"] = int(result["requests"]) + 1
        try:
            response = publish_payload(
                payload,
                endpoint=endpoint,
                token=token,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep=sleep,
                error_label="fixtures",
                producer_attestation=producer_attestation,
            )
            if not isinstance(response, Mapping):
                raise ValueError("fixture registration response must be a JSON object")
            _validate_fixture_response(response, row_count=len(payload["fixtures"]))
            for key in ("accepted", "created", "updated", "observationsAccepted", "observationsSkippedDuplicate"):
                result[key] = int(result[key]) + int(response.get(key, 0) or 0)
            if receipt_store is None:
                raise AssertionError("receipt store unavailable outside dry-run")
            response_summary = _response_summary(response)
            receipt_store.append_completed({
                **receipt_identity,
                "batchIndex": index,
                "rowCount": len(payload["fixtures"]),
                "batchDigest": batch_digest,
                "payloadSha256": payload_digest,
                "completedAt": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "response": response_summary,
                "responseSha256": _sha256(
                    _canonical_json(response_summary).encode("utf-8")
                ),
            })
            completed[receipt_key] = {
                **receipt_identity,
                "rowCount": len(payload["fixtures"]),
                "batchDigest": batch_digest,
            }
        except Exception as exc:  # transport classification is handled by publish_payload; continue other batches explicitly.
            result["failed"] = int(result["failed"]) + 1
            errors = result["errors"]
            if isinstance(errors, list):
                errors.append({"batchIndex": index, "error": str(exc)[:512]})
    if int(result["failed"]) > 0:
        result["status"] = "partial"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root_default = (
        Path(os.environ["MATCHLINE_RUNTIME_DIR"])
        if os.environ.get("MATCHLINE_RUNTIME_DIR")
        else None
    )
    parser.add_argument("snapshot", type=Path, nargs="?", default=Path("data/live/current.json"))
    parser.add_argument("--batch-size", type=int, default=MAX_FIXTURES_PER_REQUEST)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publication-epoch",
        default=os.environ.get("MATCHLINE_PUBLICATION_EPOCH"),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--retry-base-seconds", type=float, default=DEFAULT_RETRY_BASE_SECONDS)
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        help="durable OpenFootball raw archive required for non-dry-run publication",
    )
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
    endpoint = os.environ.get("MATCHLINE_FIXTURES_REGISTER_URL", "")
    token = os.environ.get("MATCHLINE_INGEST_TOKEN", "")
    if not args.dry_run and (not endpoint or not token):
        print("publish failed: MATCHLINE_FIXTURES_REGISTER_URL and MATCHLINE_INGEST_TOKEN are required", file=sys.stderr)
        return 1
    try:
        maturity_context = None
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
                live_snapshot_path=args.snapshot,
                offline_snapshot_path=args.maturity_offline_snapshot,
                sites_build_evidence_path=args.maturity_sites_build_evidence,
                test_evidence_path=args.maturity_test_evidence,
                publication_audit_path=args.maturity_publication_audit,
                prediction_archive_path=args.maturity_prediction_archive,
                publication_diagnostic_path=args.snapshot.with_name(
                    "current.publication.json"
                ),
                runtime_root=args.maturity_runtime_root,
                repository_root=args.maturity_repository_root,
            )
        elif not args.dry_run:
            raise ValueError("complete maturity artifact paths are required")
        result = publish_snapshot(
            args.snapshot,
            endpoint=endpoint or "https://invalid.example/api/fixtures/register",
            token=token,
            receipts_path=args.receipts,
            publication_epoch=args.publication_epoch,
            maturity_context=maturity_context,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            limit=args.limit,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            openfootball_raw_archive_dir=args.openfootball_raw_archive_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if int(result["failed"]) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_fixture_batch_payload",
    "build_fixture_registration_item",
    "fixture_publication_identity",
    "iter_fixture_batches",
    "publish_snapshot",
]
