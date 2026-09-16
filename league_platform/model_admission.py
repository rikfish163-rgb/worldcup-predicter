"""Local-only model admission from verified OpenFootball raw evidence.

The objects in this module are process-internal capabilities.  They can only be
created by replaying the content-addressed raw archive at an explicit cutoff;
serialized receipts, authorization strings, registry values and snapshot flags
are deliberately not part of the API.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)
from league_platform.runtime_paths import RuntimePaths
from league_platform.source_rights import (
    POLICY_VERSION,
    Decision,
    SourceId,
    SourceRightsBlocked,
    UseCase,
    require_source_rights,
)


CORPUS_SCHEMA_VERSION: Final = "matchline.verified_openfootball_corpus.v1"
_RAW_ARCHIVE_SCHEMA_VERSION: Final = "1.0.0"
_REQUIRED_USE_CASES: Final = (UseCase.TRAINING, UseCase.MODEL_INPUT)
_CURRENT_SOURCE_IDS: Final = frozenset(OPENFOOTBALL_CURRENT_SOURCE_IDS)
_HISTORICAL_SOURCE_IDS: Final = frozenset(OPENFOOTBALL_HISTORY_SOURCE_IDS)
_HEX_DIGITS: Final = frozenset("0123456789abcdef")


class ModelAdmissionBlocked(PermissionError):
    """Fixed fail-closed result for an unverified model corpus request."""

    def __init__(
        self,
        reason_code: str,
        *,
        rights_decision: Decision | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.rights_decision = rights_decision
        super().__init__(f"model admission blocked: {reason_code}")


@dataclass(frozen=True, slots=True)
class SelectedRawObservation:
    """One raw observation selected by the inclusive replay cutoff."""

    source_id: str
    retrieved_at: datetime
    record_sha256: str
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class SourceRightsEvidence:
    """The central policy decision applied to one raw source and use case."""

    source_id: str
    policy_source_id: SourceId
    use_case: UseCase
    decision: Decision


@dataclass(frozen=True, slots=True, init=False)
class VerifiedOpenFootballCorpus:
    """Immutable rows admitted by an on-site raw replay and central policy."""

    schema_version: str
    policy_version: str
    observed_before: datetime
    source_ids: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]
    selected_observations: tuple[SelectedRawObservation, ...]
    rights_evidence: tuple[SourceRightsEvidence, ...]
    admission_sha256: str
    source_manifest_sha256: str
    parser_contract_sha256: str
    rows_sha256: str
    identity_sha256: str

    def __init__(self, **_untrusted: object) -> None:
        raise TypeError(
            "VerifiedOpenFootballCorpus must be created by load_verified_openfootball_corpus"
        )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value).issubset(_HEX_DIGITS)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ModelAdmissionBlocked("invalid_observed_before")
    return value.astimezone(timezone.utc)


def _archive_root(value: Path | str | RuntimePaths) -> Path:
    if isinstance(value, RuntimePaths):
        return value.openfootball_raw_archive_dir
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise ModelAdmissionBlocked("invalid_archive_root")


def _source_id_tuple(source_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(source_ids, str):
        raise ModelAdmissionBlocked("invalid_source_ids")
    try:
        selected = tuple(source_ids)
    except TypeError as exc:
        raise ModelAdmissionBlocked("invalid_source_ids") from exc
    if (
        not selected
        or any(not isinstance(source_id, str) or not source_id for source_id in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ModelAdmissionBlocked("invalid_source_ids")
    return tuple(sorted(selected))


def _policy_source_id(source_id: str) -> SourceId | str:
    if source_id in _CURRENT_SOURCE_IDS:
        return SourceId.OPENFOOTBALL_CURRENT
    if source_id in _HISTORICAL_SOURCE_IDS:
        return SourceId.OPENFOOTBALL_HISTORICAL
    return source_id


def _rights_evidence(source_ids: tuple[str, ...]) -> tuple[SourceRightsEvidence, ...]:
    evidence: list[SourceRightsEvidence] = []
    for source_id in source_ids:
        policy_source = _policy_source_id(source_id)
        for use_case in _REQUIRED_USE_CASES:
            try:
                decision = require_source_rights(policy_source, use_case)
            except SourceRightsBlocked as exc:
                raise ModelAdmissionBlocked(
                    "source_rights_blocked",
                    rights_decision=exc.decision,
                ) from exc
            if not isinstance(policy_source, SourceId) or policy_source not in {
                SourceId.OPENFOOTBALL_CURRENT,
                SourceId.OPENFOOTBALL_HISTORICAL,
            }:
                raise ModelAdmissionBlocked("source_is_not_raw_openfootball")
            evidence.append(
                SourceRightsEvidence(
                    source_id=source_id,
                    policy_source_id=policy_source,
                    use_case=use_case,
                    decision=decision,
                )
            )
    return tuple(evidence)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(timezone.utc)


def _selected_observations(
    value: object,
    *,
    source_ids: tuple[str, ...],
    cutoff: datetime,
) -> tuple[SelectedRawObservation, ...]:
    if not isinstance(value, list) or len(value) != len(source_ids):
        raise ValueError("selected record set is invalid")
    observations: list[SelectedRawObservation] = []
    for expected_source_id, item in zip(source_ids, value, strict=True):
        if not isinstance(item, dict) or item.get("source_id") != expected_source_id:
            raise ValueError("selected record source is invalid")
        retrieved_at = _parse_timestamp(item.get("retrieved_at"))
        record_sha256 = item.get("record_sha256")
        raw_sha256 = item.get("raw_sha256")
        if (
            retrieved_at > cutoff
            or not _valid_sha256(record_sha256)
            or not _valid_sha256(raw_sha256)
        ):
            raise ValueError("selected record identity is invalid")
        record_sha256 = cast(str, record_sha256)
        raw_sha256 = cast(str, raw_sha256)
        observations.append(
            SelectedRawObservation(
                source_id=expected_source_id,
                retrieved_at=retrieved_at,
                record_sha256=record_sha256,
                raw_sha256=raw_sha256,
            )
        )
    return tuple(observations)


def _validate_rows(value: object, *, source_ids: tuple[str, ...]) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("raw replay rows are invalid")
    rows = value
    for row in rows:
        source = row.get("source")
        if not isinstance(source, dict) or source.get("source_id") not in source_ids:
            raise ValueError("raw replay row source is invalid")
    return rows


def _validated_loader_result(
    value: object,
    *,
    source_ids: tuple[str, ...],
    cutoff: datetime,
) -> tuple[
    list[dict[str, object]],
    tuple[SelectedRawObservation, ...],
    str,
    str,
    str,
    str,
]:
    try:
        if not isinstance(value, dict):
            raise ValueError("raw replay result is not an object")
        if (
            value.get("schema_version") != _RAW_ARCHIVE_SCHEMA_VERSION
            or value.get("status") != "verified_raw_candidate"
            or value.get("observed_before") != cutoff.isoformat()
            or value.get("source_ids") != list(source_ids)
        ):
            raise ValueError("raw replay header is invalid")
        selected = _selected_observations(
            value.get("selected_records"),
            source_ids=source_ids,
            cutoff=cutoff,
        )
        rows = _validate_rows(value.get("rows"), source_ids=source_ids)
        if not rows:
            raise ModelAdmissionBlocked("verified_corpus_empty")
        source_manifest = value.get("source_manifest")
        parser_contract = value.get("parser_contract")
        if not isinstance(source_manifest, list) or not isinstance(parser_contract, dict):
            raise ValueError("raw replay contracts are invalid")

        source_manifest_sha256 = _sha256_json(source_manifest)
        parser_contract_sha256 = _sha256_json(parser_contract)
        rows_sha256 = _sha256_json(rows)
        if (
            value.get("source_manifest_sha256") != source_manifest_sha256
            or value.get("parser_contract_sha256") != parser_contract_sha256
            or value.get("rows_sha256") != rows_sha256
        ):
            raise ValueError("raw replay component hash is invalid")

        admission_identity = {
            "schema_version": _RAW_ARCHIVE_SCHEMA_VERSION,
            "observed_before": cutoff.isoformat(),
            "source_ids": list(source_ids),
            "selected_record_sha256": [item.record_sha256 for item in selected],
            "source_manifest_sha256": source_manifest_sha256,
            "parser_contract_sha256": parser_contract_sha256,
            "rows_sha256": rows_sha256,
        }
        admission_sha256 = _sha256_json(admission_identity)
        if value.get("admission_sha256") != admission_sha256:
            raise ValueError("raw replay admission hash is invalid")
    except (TypeError, ValueError) as exc:
        raise ModelAdmissionBlocked("raw_archive_contract_invalid") from exc
    return (
        rows,
        selected,
        admission_sha256,
        source_manifest_sha256,
        parser_contract_sha256,
        rows_sha256,
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ModelAdmissionBlocked("raw_archive_contract_invalid")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ModelAdmissionBlocked("raw_archive_contract_invalid")


def _frozen_rows(rows: list[dict[str, object]]) -> tuple[Mapping[str, object], ...]:
    frozen: list[Mapping[str, object]] = []
    for row in rows:
        item = _freeze_json(row)
        if not isinstance(item, Mapping):
            raise ModelAdmissionBlocked("raw_archive_contract_invalid")
        frozen.append(item)
    return tuple(frozen)


def _corpus_identity(
    *,
    cutoff: datetime,
    source_ids: tuple[str, ...],
    selected: tuple[SelectedRawObservation, ...],
    rights_evidence: tuple[SourceRightsEvidence, ...],
    admission_sha256: str,
    source_manifest_sha256: str,
    parser_contract_sha256: str,
    rows_sha256: str,
) -> str:
    identity = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "observed_before": cutoff.isoformat(),
        "source_ids": list(source_ids),
        "required_use_cases": [use_case.value for use_case in _REQUIRED_USE_CASES],
        "selected_observations": [
            {
                "source_id": item.source_id,
                "retrieved_at": item.retrieved_at.isoformat(),
                "record_sha256": item.record_sha256,
                "raw_sha256": item.raw_sha256,
            }
            for item in selected
        ],
        "rights_evidence": [
            {
                "source_id": item.source_id,
                "policy_source_id": item.policy_source_id.value,
                "use_case": item.use_case.value,
                "decision": item.decision.as_dict(),
            }
            for item in rights_evidence
        ],
        "admission_sha256": admission_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "parser_contract_sha256": parser_contract_sha256,
        "rows_sha256": rows_sha256,
    }
    return _sha256_json(identity)


def _build_corpus(
    *,
    cutoff: datetime,
    source_ids: tuple[str, ...],
    rows: list[dict[str, object]],
    selected: tuple[SelectedRawObservation, ...],
    rights_evidence: tuple[SourceRightsEvidence, ...],
    admission_sha256: str,
    source_manifest_sha256: str,
    parser_contract_sha256: str,
    rows_sha256: str,
) -> VerifiedOpenFootballCorpus:
    corpus = object.__new__(VerifiedOpenFootballCorpus)
    values: dict[str, object] = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "observed_before": cutoff,
        "source_ids": source_ids,
        "rows": _frozen_rows(rows),
        "selected_observations": selected,
        "rights_evidence": rights_evidence,
        "admission_sha256": admission_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "parser_contract_sha256": parser_contract_sha256,
        "rows_sha256": rows_sha256,
        "identity_sha256": _corpus_identity(
            cutoff=cutoff,
            source_ids=source_ids,
            selected=selected,
            rights_evidence=rights_evidence,
            admission_sha256=admission_sha256,
            source_manifest_sha256=source_manifest_sha256,
            parser_contract_sha256=parser_contract_sha256,
            rows_sha256=rows_sha256,
        ),
    }
    for field, value in values.items():
        object.__setattr__(corpus, field, value)
    return corpus


def load_verified_openfootball_corpus(
    archive_root: Path | str | RuntimePaths,
    *,
    source_ids: Iterable[str],
    observed_before: datetime,
) -> VerifiedOpenFootballCorpus:
    """Replay raw evidence and return an immutable model/training corpus.

    Both ``training`` and ``model_input`` rights are enforced for every raw
    source before archive I/O.  The loader accepts no serialized trust or
    authorization input, and it never opens the network.
    """

    root = _archive_root(archive_root)
    cutoff = _utc(observed_before)
    selected_ids = _source_id_tuple(source_ids)
    rights_evidence = _rights_evidence(selected_ids)
    try:
        loader_result = load_verified_openfootball_archive(
            root,
            source_ids=selected_ids,
            observed_before=cutoff,
        )
    except (OpenFootballRawArchiveError, OSError, TypeError, ValueError) as exc:
        raise ModelAdmissionBlocked("raw_archive_verification_failed") from exc
    (
        rows,
        selected,
        admission_sha256,
        source_manifest_sha256,
        parser_contract_sha256,
        rows_sha256,
    ) = _validated_loader_result(
        loader_result,
        source_ids=selected_ids,
        cutoff=cutoff,
    )
    return _build_corpus(
        cutoff=cutoff,
        source_ids=selected_ids,
        rows=rows,
        selected=selected,
        rights_evidence=rights_evidence,
        admission_sha256=admission_sha256,
        source_manifest_sha256=source_manifest_sha256,
        parser_contract_sha256=parser_contract_sha256,
        rows_sha256=rows_sha256,
    )


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "ModelAdmissionBlocked",
    "SelectedRawObservation",
    "SourceRightsEvidence",
    "VerifiedOpenFootballCorpus",
    "load_verified_openfootball_corpus",
]
