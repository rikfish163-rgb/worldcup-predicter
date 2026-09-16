"""Historical training matches admitted only from verified OpenFootball raw bytes."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from league_platform.domain import KickoffTimeQuality, Match, Score, SourceResult
from league_platform.identity import team_id
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)
from league_platform.source_rights import SourceId, UseCase, require_source_rights


ADMISSION_SCHEMA_VERSION = "matchline.openfootball_verified_history_admission.v1"
ADAPTER_CONTRACT_VERSION = "1.0.0"
RAW_CANDIDATE_FIELDS = {
    "schema_version",
    "status",
    "training_admitted",
    "observed_before",
    "source_ids",
    "selected_records",
    "source_manifest",
    "parser_contract",
    "rows",
    "manifest_sha256",
    "source_manifest_sha256",
    "parser_contract_sha256",
    "rows_sha256",
    "admission_sha256",
}
EXPECTED_PARSER_CONTRACT = {
    "json": "openfootball_json_v1",
    "txt": "openfootball_football_txt_v1",
}


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OpenFootballRawArchiveError("domain admission value is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenFootballRawArchiveError(f"{field} must be an object")
    return value


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OpenFootballRawArchiveError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpenFootballRawArchiveError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpenFootballRawArchiveError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _score(value: object, halftime: object) -> Score:
    score = _mapping(value, field="row.score")
    if set(score) != {"home", "away"}:
        raise OpenFootballRawArchiveError("row.score fields are invalid")
    home = score.get("home")
    away = score.get("away")
    if (
        not isinstance(home, int)
        or isinstance(home, bool)
        or home < 0
        or not isinstance(away, int)
        or isinstance(away, bool)
        or away < 0
    ):
        raise OpenFootballRawArchiveError("row.score is invalid")
    halftime_home = halftime_away = None
    if halftime is not None:
        halftime_score = _mapping(halftime, field="row.halftime_score")
        if set(halftime_score) != {"home", "away"}:
            raise OpenFootballRawArchiveError("row.halftime_score fields are invalid")
        halftime_home = halftime_score.get("home")
        halftime_away = halftime_score.get("away")
        if (
            not isinstance(halftime_home, int)
            or isinstance(halftime_home, bool)
            or halftime_home < 0
            or halftime_home > home
            or not isinstance(halftime_away, int)
            or isinstance(halftime_away, bool)
            or halftime_away < 0
            or halftime_away > away
        ):
            raise OpenFootballRawArchiveError("row.halftime_score is invalid")
    return Score(
        home=home,
        away=away,
        halftime_home=halftime_home,
        halftime_away=halftime_away,
    )


def _kickoff(
    row: Mapping[str, Any], *, expected_timezone_name: str
) -> tuple[datetime, KickoffTimeQuality]:
    if row.get("kickoff_timezone") != expected_timezone_name:
        raise OpenFootballRawArchiveError("candidate kickoff timezone is invalid")
    quality = row.get("kickoff_time_quality")
    if quality == "exact":
        return _timestamp(row.get("kickoff_at"), field="candidate row kickoff_at"), "exact"
    if quality != "date_only":
        raise OpenFootballRawArchiveError("candidate kickoff time quality is invalid")
    raw_date = row.get("kickoff_date")
    timezone_name = row.get("kickoff_timezone")
    if not isinstance(raw_date, str) or not isinstance(timezone_name, str):
        raise OpenFootballRawArchiveError("date-only kickoff identity is invalid")
    try:
        kickoff_date = date.fromisoformat(raw_date)
        local_timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise OpenFootballRawArchiveError("date-only kickoff identity is invalid") from exc
    return datetime.combine(kickoff_date, time.min, tzinfo=local_timezone), "date_only"


def _validate_row_provenance(
    row: Mapping[str, Any],
    *,
    source_id: str,
    source: Mapping[str, Any],
    selected_record: Mapping[str, Any],
    source_config: Mapping[str, str],
) -> None:
    raw_sha = selected_record.get("raw_sha256")
    retrieved_at = selected_record.get("retrieved_at")
    expected_source = {
        "name": "OpenFootball",
        "source_id": source_id,
        "url": source_config["url"],
        "retrieved_at": retrieved_at,
        "raw_sha256": raw_sha,
        "hash": f"sha256:{raw_sha}",
        "license": "CC0-1.0",
    }
    if source != expected_source:
        raise OpenFootballRawArchiveError("candidate row source provenance is invalid")
    lineage = _mapping(row.get("lineage"), field="candidate row lineage")
    expected_lineage_fields = {
        "source_id",
        "url",
        "retrieved_at",
        "raw_sha256",
        "hash",
        "license",
        "parser",
        "record_index",
        "line_number",
        "record_path",
        "source_row_id",
        "date_inherited",
        "time_inherited",
    }
    if set(lineage) != expected_lineage_fields:
        raise OpenFootballRawArchiveError("candidate row lineage provenance is invalid")
    expected_core = {
        "source_id": source_id,
        "url": source_config["url"],
        "retrieved_at": retrieved_at,
        "raw_sha256": raw_sha,
        "hash": f"sha256:{raw_sha}",
        "license": "CC0-1.0",
        "parser": EXPECTED_PARSER_CONTRACT[source_config["format"]],
    }
    if any(lineage.get(key) != value for key, value in expected_core.items()):
        raise OpenFootballRawArchiveError("candidate row lineage provenance is invalid")
    record_index = lineage.get("record_index")
    line_number = lineage.get("line_number")
    if (
        isinstance(record_index, bool)
        or not isinstance(record_index, int)
        or record_index < 0
        or isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
        or not isinstance(lineage.get("date_inherited"), bool)
        or not isinstance(lineage.get("time_inherited"), bool)
    ):
        raise OpenFootballRawArchiveError("candidate row lineage pointer is invalid")
    expected_record_path = (
        f"$.matches[{record_index}]" if source_config["format"] == "json" else f"L{line_number}"
    )
    if (
        lineage.get("record_path") != expected_record_path
        or lineage.get("source_row_id") != f"{source_id}#{expected_record_path}"
    ):
        raise OpenFootballRawArchiveError("candidate row lineage pointer is invalid")


class VerifiedOpenFootballHistorySource:
    """Project verified raw-history candidates into the training domain."""

    def __init__(
        self,
        archive_dir: Path | str,
        observed_before: datetime,
        source_ids: Iterable[str] = OPENFOOTBALL_HISTORY_SOURCE_IDS,
    ) -> None:
        selected = tuple(source_ids)
        allowed = set(OPENFOOTBALL_HISTORY_SOURCE_IDS)
        if not selected or len(set(selected)) != len(selected):
            raise OpenFootballRawArchiveError("historical source_ids must be unique and non-empty")
        if any(source_id not in allowed for source_id in selected):
            raise OpenFootballRawArchiveError("source_ids must use the historical allowlist")
        self._candidate = load_verified_openfootball_archive(
            archive_dir,
            source_ids=selected,
            observed_before=observed_before,
        )
        self._observed_before = _timestamp(
            self._candidate.get("observed_before"), field="candidate.observed_before"
        )
        if (
            not isinstance(observed_before, datetime)
            or observed_before.tzinfo is None
            or observed_before.utcoffset() is None
            or self._observed_before != observed_before.astimezone(timezone.utc)
        ):
            raise OpenFootballRawArchiveError(
                "raw candidate cutoff does not match requested observed_before"
            )
        if (
            self._candidate.get("status") != "verified_raw_candidate"
            or self._candidate.get("training_admitted") is not False
        ):
            raise OpenFootballRawArchiveError("raw candidate admission contract is invalid")
        self._validate_candidate_digests(self._candidate, selected)
        source_identity, training_policy = self._validated_source_manifest(
            self._candidate, selected
        )
        self._matches, self._projection_counts = self._project_matches(self._candidate)
        if not self._matches:
            raise OpenFootballRawArchiveError(
                "verified raw archive has no finished matches for training"
            )
        self._competitions = {
            OPENFOOTBALL_HISTORY_SOURCES[source_id]["competition_id"] for source_id in selected
        }
        self._admission_manifest = self._build_admission_manifest(
            source_identity=source_identity,
            training_policy=training_policy,
        )

    @staticmethod
    def _validate_candidate_digests(
        candidate: Mapping[str, Any], selected: tuple[str, ...]
    ) -> None:
        if set(candidate) != RAW_CANDIDATE_FIELDS:
            raise OpenFootballRawArchiveError("raw candidate fields are invalid")
        if candidate.get("schema_version") != "1.0.0":
            raise OpenFootballRawArchiveError("raw candidate schema is unsupported")
        if candidate.get("parser_contract") != EXPECTED_PARSER_CONTRACT:
            raise OpenFootballRawArchiveError("raw candidate parser contract is unsupported")
        selected_ids = sorted(selected)
        if candidate.get("source_ids") != selected_ids:
            raise OpenFootballRawArchiveError("raw candidate source_ids do not match request")
        for field in (
            "manifest_sha256",
            "source_manifest_sha256",
            "parser_contract_sha256",
            "rows_sha256",
            "admission_sha256",
        ):
            if not _valid_digest(candidate.get(field)):
                raise OpenFootballRawArchiveError(f"candidate.{field} is invalid")
        digest_values = (
            ("source_manifest", "source_manifest_sha256"),
            ("parser_contract", "parser_contract_sha256"),
            ("rows", "rows_sha256"),
        )
        for value_field, digest_field in digest_values:
            if _canonical_sha256(candidate.get(value_field)) != candidate.get(digest_field):
                raise OpenFootballRawArchiveError(
                    f"candidate.{digest_field} does not match {value_field}"
                )
        records = candidate.get("selected_records")
        if not isinstance(records, list) or len(records) != len(selected_ids):
            raise OpenFootballRawArchiveError("candidate.selected_records are invalid")
        record_digests: list[str] = []
        record_ids: list[str] = []
        for raw_record in records:
            record = _mapping(raw_record, field="candidate selected record")
            if set(record) != {
                "source_id",
                "retrieved_at",
                "record_sha256",
                "raw_sha256",
            }:
                raise OpenFootballRawArchiveError("candidate selected record fields are invalid")
            source_id = record.get("source_id")
            record_sha = record.get("record_sha256")
            if (
                not isinstance(source_id, str)
                or not isinstance(record_sha, str)
                or not _valid_digest(record_sha)
            ):
                raise OpenFootballRawArchiveError("candidate selected record identity is invalid")
            if not _valid_digest(record.get("raw_sha256")):
                raise OpenFootballRawArchiveError(
                    "candidate selected record raw digest is invalid"
                )
            _timestamp(record.get("retrieved_at"), field="candidate selected record retrieved_at")
            record_ids.append(source_id)
            record_digests.append(record_sha)
        if record_ids != selected_ids:
            raise OpenFootballRawArchiveError("candidate selected records do not match source_ids")
        admission_identity = {
            "schema_version": candidate.get("schema_version"),
            "observed_before": candidate.get("observed_before"),
            "source_ids": selected_ids,
            "selected_record_sha256": record_digests,
            "source_manifest_sha256": candidate.get("source_manifest_sha256"),
            "parser_contract_sha256": candidate.get("parser_contract_sha256"),
            "rows_sha256": candidate.get("rows_sha256"),
        }
        if _canonical_sha256(admission_identity) != candidate.get("admission_sha256"):
            raise OpenFootballRawArchiveError(
                "candidate.admission_sha256 does not match raw admission"
            )

    @staticmethod
    def _validated_source_manifest(
        candidate: Mapping[str, Any], selected: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw_manifest = candidate.get("source_manifest")
        if not isinstance(raw_manifest, list):
            raise OpenFootballRawArchiveError("candidate.source_manifest must be a list")
        entries: dict[str, Mapping[str, Any]] = {}
        for raw_entry in raw_manifest:
            entry = _mapping(raw_entry, field="candidate source manifest entry")
            source_id = entry.get("source_id")
            if not isinstance(source_id, str) or source_id in entries:
                raise OpenFootballRawArchiveError("candidate source manifest identity is invalid")
            entries[source_id] = entry
        if set(entries) != set(selected):
            raise OpenFootballRawArchiveError(
                "candidate source manifest does not match source_ids"
            )
        training_decision = require_source_rights(
            SourceId.OPENFOOTBALL_HISTORICAL,
            UseCase.TRAINING,
        ).as_dict()
        source_identity: list[dict[str, Any]] = []
        training_policy: list[dict[str, Any]] = []
        for source_id in sorted(selected):
            entry = entries[source_id]
            config = entry.get("source_config")
            if config != OPENFOOTBALL_HISTORY_SOURCES[source_id]:
                raise OpenFootballRawArchiveError("candidate source configuration is invalid")
            if entry.get("license") != "CC0-1.0":
                raise OpenFootballRawArchiveError("candidate source license is invalid")
            if entry.get("policy_source_id") != SourceId.OPENFOOTBALL_HISTORICAL.value:
                raise OpenFootballRawArchiveError("candidate source policy identity is invalid")
            policy_decisions = _mapping(
                entry.get("policy_decisions"), field="candidate source policy decisions"
            )
            if policy_decisions.get(UseCase.TRAINING.value) != training_decision:
                raise OpenFootballRawArchiveError("candidate training policy decision is invalid")
            source_identity.append(
                {
                    "source_id": source_id,
                    "source_config": dict(config),
                    "license": "CC0-1.0",
                }
            )
            training_policy.append(
                {
                    "source_id": source_id,
                    "policy_source_id": SourceId.OPENFOOTBALL_HISTORICAL.value,
                    "training": training_decision,
                }
            )
        return source_identity, training_policy

    @staticmethod
    def _project_matches(candidate: Mapping[str, Any]) -> tuple[list[Match], dict[str, Any]]:
        raw_rows = candidate.get("rows")
        if not isinstance(raw_rows, list):
            raise OpenFootballRawArchiveError("candidate.rows must be a list")
        raw_selected_records = candidate.get("selected_records")
        if not isinstance(raw_selected_records, list):
            raise OpenFootballRawArchiveError("candidate.selected_records must be a list")
        selected_records: dict[str, Mapping[str, Any]] = {}
        for raw_record in raw_selected_records:
            record = _mapping(raw_record, field="candidate selected record")
            source_id = record.get("source_id")
            if not isinstance(source_id, str) or source_id in selected_records:
                raise OpenFootballRawArchiveError("candidate selected record identity is invalid")
            selected_records[source_id] = record
        matches: list[Match] = []
        upcoming = 0
        exact = 0
        date_only = 0
        seen_fixture_ids: set[str] = set()
        for raw_row in raw_rows:
            row = _mapping(raw_row, field="candidate row")
            fixture_id = row.get("id")
            if not isinstance(fixture_id, str) or not fixture_id:
                raise OpenFootballRawArchiveError("candidate row fixture id is invalid")
            if fixture_id in seen_fixture_ids:
                raise OpenFootballRawArchiveError("duplicate domain fixture id")
            seen_fixture_ids.add(fixture_id)
            if row.get("status") == "upcoming":
                upcoming += 1
                continue
            if row.get("status") != "finished":
                raise OpenFootballRawArchiveError("candidate row status is invalid")
            competition_id = row.get("competition_id")
            season = row.get("season")
            home = row.get("home_team")
            away = row.get("away_team")
            if (
                not isinstance(competition_id, str)
                or not competition_id
                or not isinstance(season, str)
                or not season
                or not isinstance(home, str)
                or not home
                or not isinstance(away, str)
                or not away
            ):
                raise OpenFootballRawArchiveError("candidate row identity is invalid")
            source = _mapping(row.get("source"), field="candidate row source")
            source_id = source.get("source_id")
            if not isinstance(source_id, str) or source_id not in OPENFOOTBALL_HISTORY_SOURCES:
                raise OpenFootballRawArchiveError("candidate row source_id is invalid")
            source_config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
            if competition_id != source_config["competition_id"]:
                raise OpenFootballRawArchiveError("row competition does not match source")
            if season != source_config["season"]:
                raise OpenFootballRawArchiveError("row season does not match source")
            raw_sha = source.get("raw_sha256")
            retrieved_at = source.get("retrieved_at")
            selected_record = selected_records.get(source_id)
            if selected_record is None or (
                raw_sha != selected_record.get("raw_sha256")
                or retrieved_at != selected_record.get("retrieved_at")
            ):
                raise OpenFootballRawArchiveError(
                    "row raw identity does not match selected record"
                )
            _validate_row_provenance(
                row,
                source_id=source_id,
                source=source,
                selected_record=selected_record,
                source_config=source_config,
            )
            provider_ids = _mapping(
                row.get("provider_fixture_ids"), field="candidate provider fixture ids"
            )
            if provider_ids != {"OpenFootball": fixture_id}:
                raise OpenFootballRawArchiveError("candidate provider fixture id is invalid")
            if not isinstance(raw_sha, str) or len(raw_sha) != 64:
                raise OpenFootballRawArchiveError("candidate raw SHA-256 is invalid")
            kickoff, kickoff_quality = _kickoff(
                row,
                expected_timezone_name=source_config["timezone"],
            )
            observed_at = _timestamp(retrieved_at, field="candidate row retrieved_at")
            future_result = (
                kickoff > observed_at
                if kickoff_quality == "exact"
                else kickoff.date() >= observed_at.astimezone(kickoff.tzinfo).date()
            )
            if future_result:
                raise OpenFootballRawArchiveError("finished result was observed before kickoff")
            if kickoff_quality == "exact":
                exact += 1
            else:
                date_only += 1
            match_score = _score(row.get("score"), row.get("halftime_score"))
            matches.append(
                Match(
                    id=fixture_id,
                    competition_id=competition_id,
                    season=season,
                    kickoff_at=kickoff,
                    home_team=home,
                    away_team=away,
                    home_team_id=team_id(competition_id, home),
                    away_team_id=team_id(competition_id, away),
                    status="finished",
                    score=match_score,
                    market_probability=None,
                    source_file=f"raw/sha256/{raw_sha[:2]}/{raw_sha}.raw",
                    source_sha256=raw_sha,
                    provider_fixture_id=fixture_id,
                    source_name="OpenFootball",
                    source_license_status="CC0-1.0",
                    kickoff_time_quality=kickoff_quality,
                    kickoff_time_source=str(row.get("kickoff_time_source") or ""),
                    # The raw retrieval is a 2026 archive observation, not the
                    # historical event time.  ``None`` lets chronological
                    # evaluators apply results after the same-kickoff batch.
                    kickoff_time_observed_at=None,
                    result_observed_at=None,
                    market_time_basis="unavailable",
                )
            )
        matches.sort(key=lambda match: (match.kickoff_at, match.id))
        return matches, {
            "raw_rows": len(raw_rows),
            "finished_admitted": len(matches),
            "upcoming_isolated": upcoming,
            "exact_kickoff_admitted": exact,
            "date_only_admitted": date_only,
            "competition_rows": dict(
                sorted(Counter(match.competition_id for match in matches).items())
            ),
        }

    def _build_admission_manifest(
        self,
        *,
        source_identity: list[dict[str, Any]],
        training_policy: list[dict[str, Any]],
    ) -> dict[str, Any]:
        candidate = self._candidate
        domain_rows = [
            match.to_dict() for match in sorted(self._matches, key=lambda match: match.id)
        ]
        manifest: dict[str, Any] = {
            "schema_version": ADMISSION_SCHEMA_VERSION,
            "status": "training_admitted",
            "training_admitted": True,
            "training_source": "verified_openfootball_raw_archive_only",
            "admission_scope": "historical_training",
            "loader_contract_version": candidate.get("schema_version"),
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "observed_before": candidate.get("observed_before"),
            "source_ids": list(candidate.get("source_ids") or []),
            "competition_ids": sorted(self._competitions),
            "selected_records": copy.deepcopy(candidate.get("selected_records") or []),
            "raw_admission_sha256": candidate.get("admission_sha256"),
            "raw_manifest_sha256": candidate.get("manifest_sha256"),
            "source_manifest_sha256": candidate.get("source_manifest_sha256"),
            "source_identity_sha256": _canonical_sha256(source_identity),
            "training_policy_sha256": _canonical_sha256(training_policy),
            "parser_contract_sha256": candidate.get("parser_contract_sha256"),
            "raw_rows_sha256": candidate.get("rows_sha256"),
            "domain_rows_sha256": _canonical_sha256(domain_rows),
            "counts": copy.deepcopy(self._projection_counts),
            "statement": (
                "Only rows bound by this manifest are admitted as OpenFootball "
                "historical training data."
            ),
        }
        identity = {
            key: value
            for key, value in manifest.items()
            if key not in {"raw_manifest_sha256", "statement"}
        }
        manifest["admission_sha256"] = _canonical_sha256(identity)
        return manifest

    def admission_manifest(self) -> dict[str, Any]:
        """Return a detached, content-addressed training-admission receipt."""

        return copy.deepcopy(self._admission_manifest)

    def load(self, competition_id: str) -> SourceResult:
        if competition_id not in self._competitions:
            raise OpenFootballRawArchiveError("competition_id is outside admitted sources")
        matches = [match for match in self._matches if match.competition_id == competition_id]
        latest = max((match.kickoff_at for match in matches), default=None)
        return SourceResult(
            competition_id=competition_id,
            status="stale",
            matches=matches,
            latest_event_at=latest,
            message="Verified OpenFootball raw archive; historical training only.",
            quality={
                "row_count": len(matches),
                "date_only_kickoff_rows": sum(
                    match.kickoff_time_quality == "date_only" for match in matches
                ),
                "duplicate_fixture_rate": 0.0,
                "score_completeness": 1.0 if matches else 0.0,
                "odds_completeness": 0.0,
            },
        )


__all__ = ["VerifiedOpenFootballHistorySource"]
