"""Load causally observed finished results from immutable live snapshots.

The live synchroniser keeps a bounded lookback in every canonical fixture snapshot.  This
module turns those finished rows into the same domain contract used by the
historical sources, while retaining the snapshot observation time.  A result
is not allowed to update a prospective model before that observation time.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from league_platform.catalog import LEAGUES
from league_platform.domain import Match, Score
from league_platform.fixture_feed import fixture_rows
from league_platform.identity import team_id
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchiveError,
    load_verified_openfootball_archive,
)
from league_platform.source_rights import SourceId, UseCase, require_source_rights


_COMPETITIONS = frozenset(league.id for league in LEAGUES)


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be timezone-aware")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _paths(live_path: Path, archive_dir: Path) -> list[Path]:
    paths = [live_path]
    raw_dir = archive_dir / "raw"
    if raw_dir.exists():
        paths.extend(sorted(raw_dir.glob("*.json")))
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        result.append(path)
    return result


def _empty_admission() -> dict[str, Any]:
    return {
        "snapshots_checked": 0,
        "candidate_rows": 0,
        "admitted_rows": 0,
        "quarantined_rows": 0,
        "excluded_display_only_rows": 0,
        "verified_archive_replays": 0,
        "quarantine_reasons": Counter(),
        "exclusion_reasons": Counter(),
    }


def _quarantine(admission: dict[str, Any], reason: str, *, count: int = 1) -> None:
    if count < 1:
        return
    admission["quarantined_rows"] += count
    admission["quarantine_reasons"][reason] += count


def _exclude_display_only(admission: dict[str, Any], reason: str, *, count: int = 1) -> None:
    if count < 1:
        return
    admission["excluded_display_only_rows"] += count
    admission["exclusion_reasons"][reason] += count


def _merge_admission(target: dict[str, Any], source: dict[str, Any]) -> None:
    for field in (
        "snapshots_checked",
        "candidate_rows",
        "admitted_rows",
        "quarantined_rows",
        "excluded_display_only_rows",
        "verified_archive_replays",
    ):
        target[field] += source[field]
    target["quarantine_reasons"].update(source["quarantine_reasons"])
    target["exclusion_reasons"].update(source["exclusion_reasons"])


def _openligadb_finished_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    secondary = snapshot.get("openligadb")
    matches = secondary.get("matches") if isinstance(secondary, dict) else None
    if not isinstance(matches, list):
        return []
    return [row for row in matches if isinstance(row, dict) and row.get("status") == "finished"]


def _is_isolated_openligadb_display_row(snapshot: dict[str, Any], row: dict[str, Any]) -> bool:
    envelope = snapshot.get("openligadb")
    if not isinstance(envelope, dict):
        return False
    if (
        envelope.get("provider") != "OpenLigaDB"
        or envelope.get("display_lane") != "isolated_current_only"
        or envelope.get("fixture_feed_eligible") is not False
        or envelope.get("material_feature_eligible") is not False
        or envelope.get("model_eligible") is not False
        or envelope.get("redistribution_allowed") is not False
        or envelope.get("training_eligible") is not False
        or any(
            not isinstance(envelope.get(field), dict) or envelope[field].get("decision") != "block"
            for field in (
                "model_rights",
                "training_rights",
                "redistribution_rights",
            )
        )
    ):
        return False
    score = row.get("score")
    source = row.get("source")
    if (
        row.get("display_lane") != "isolated_current_only"
        or row.get("enters_model") is not False
        or row.get("model_eligible") is not False
        or row.get("redistribution_allowed") is not False
        or row.get("training_eligible") is not False
        or not isinstance(row.get("id"), str)
        or row.get("competition_id") not in _COMPETITIONS
        or not isinstance(row.get("home_team"), str)
        or not row["home_team"].strip()
        or not isinstance(row.get("away_team"), str)
        or not row["away_team"].strip()
        or not isinstance(score, dict)
        or any(
            isinstance(score.get(key), bool)
            or not isinstance(score.get(key), int)
            or score[key] < 0
            for key in ("home", "away")
        )
        or not isinstance(source, dict)
        or source.get("name") != "OpenLigaDB"
    ):
        return False
    try:
        _utc(row.get("kickoff_at"), field="openligadb.match.kickoff_at")
    except (TypeError, ValueError):
        return False
    return True


def _verified_source_manifest_entry(
    candidate: dict[str, Any],
    *,
    source_id: str,
) -> dict[str, Any] | None:
    manifest = candidate.get("source_manifest")
    if not isinstance(manifest, list):
        return None
    matches = [
        row for row in manifest if isinstance(row, dict) and row.get("source_id") == source_id
    ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    policy_value = entry.get("policy_source_id")
    try:
        policy_source = SourceId(policy_value)
        training = require_source_rights(policy_source, UseCase.TRAINING)
        redistribution = require_source_rights(policy_source, UseCase.REDISTRIBUTION)
    except (TypeError, ValueError, PermissionError):
        return None
    decisions = entry.get("policy_decisions")
    if (
        not isinstance(decisions, dict)
        or decisions.get(UseCase.TRAINING.value) != training.as_dict()
        or decisions.get(UseCase.REDISTRIBUTION.value) != redistribution.as_dict()
    ):
        return None
    return entry


def _verified_candidate(
    path: Path,
    *,
    reference: datetime,
    openfootball_raw_archive_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    admission = _empty_admission()
    admission["snapshots_checked"] = 1
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot root must be an object")
        observed_at = _utc(snapshot.get("as_of"), field="snapshot.as_of")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return [], admission, True

    fixtures = [
        row
        for row in fixture_rows(snapshot)
        if isinstance(row, dict) and row.get("status") == "finished"
    ]
    secondary_rows = _openligadb_finished_rows(snapshot)
    admission["candidate_rows"] = len(fixtures) + len(secondary_rows)
    isolated_secondary_rows = [
        row for row in secondary_rows if _is_isolated_openligadb_display_row(snapshot, row)
    ]
    _exclude_display_only(
        admission,
        "openligadb_display_only",
        count=len(isolated_secondary_rows),
    )
    _quarantine(
        admission,
        "openligadb_not_isolated",
        count=len(secondary_rows) - len(isolated_secondary_rows),
    )
    if observed_at > reference:
        _quarantine(
            admission,
            "snapshot_observed_after_reference",
            count=len(fixtures),
        )
        return [], admission, False

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fixture in fixtures:
        source = fixture.get("source")
        if not isinstance(source, dict) or source.get("name") != "OpenFootball":
            _quarantine(admission, "provider_not_openfootball")
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            _quarantine(admission, "openfootball_provenance_missing")
            continue
        try:
            declared_source_time = _utc(
                source.get("retrieved_at"),
                field="fixture.source.retrieved_at",
            )
            declared_kickoff = _utc(fixture.get("kickoff_at"), field="fixture.kickoff_at")
        except (TypeError, ValueError):
            _quarantine(admission, "source_timestamp_invalid")
            continue
        if declared_source_time < declared_kickoff:
            _quarantine(admission, "source_timestamp_before_kickoff")
            continue
        if declared_source_time > observed_at or declared_source_time > reference:
            _quarantine(admission, "source_timestamp_after_snapshot")
            continue
        by_source[source_id].append(fixture)

    admitted: list[dict[str, Any]] = []
    for source_id, source_rows in sorted(by_source.items()):
        try:
            candidate = load_verified_openfootball_archive(
                openfootball_raw_archive_dir,
                source_ids=[source_id],
                observed_before=observed_at,
            )
        except (OpenFootballRawArchiveError, OSError):
            _quarantine(
                admission,
                "openfootball_archive_verification_failed",
                count=len(source_rows),
            )
            continue
        admission["verified_archive_replays"] += 1
        if _verified_source_manifest_entry(candidate, source_id=source_id) is None:
            _quarantine(
                admission,
                "source_rights_not_allowed",
                count=len(source_rows),
            )
            continue
        raw_rows = candidate.get("rows")
        if not isinstance(raw_rows, list):
            _quarantine(
                admission,
                "openfootball_archive_verification_failed",
                count=len(source_rows),
            )
            continue
        raw_by_id = {
            row.get("id"): row
            for row in raw_rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        selected_records = candidate.get("selected_records")
        selected_record = (
            selected_records[0]
            if isinstance(selected_records, list)
            and len(selected_records) == 1
            and isinstance(selected_records[0], dict)
            else None
        )
        if selected_record is None:
            _quarantine(
                admission,
                "openfootball_archive_verification_failed",
                count=len(source_rows),
            )
            continue
        for fixture in source_rows:
            verified = raw_by_id.get(fixture.get("id"))
            if not isinstance(verified, dict) or verified.get("status") != "finished":
                _quarantine(admission, "verified_openfootball_row_missing")
                continue
            fixture_source = fixture.get("source")
            verified_source = verified.get("source")
            if not isinstance(fixture_source, dict) or not isinstance(verified_source, dict):
                _quarantine(admission, "openfootball_provenance_missing")
                continue
            try:
                fixture_source_time = _utc(
                    fixture_source.get("retrieved_at"),
                    field="fixture.source.retrieved_at",
                )
                verified_source_time = _utc(
                    verified_source.get("retrieved_at"),
                    field="verified.source.retrieved_at",
                )
                kickoff = _utc(verified.get("kickoff_at"), field="verified.kickoff_at")
            except (TypeError, ValueError):
                _quarantine(admission, "source_timestamp_invalid")
                continue
            if fixture_source_time != verified_source_time:
                _quarantine(admission, "source_timestamp_mismatch")
                continue
            if verified_source_time < kickoff:
                _quarantine(admission, "source_timestamp_before_kickoff")
                continue
            if verified_source_time > observed_at or verified_source_time > reference:
                _quarantine(admission, "source_timestamp_after_snapshot")
                continue
            if observed_at < kickoff:
                _quarantine(admission, "snapshot_observed_before_kickoff")
                continue
            if fixture != verified:
                _quarantine(admission, "verified_openfootball_row_mismatch")
                continue
            score = verified.get("score")
            if not isinstance(score, dict):
                _quarantine(admission, "verified_openfootball_row_mismatch")
                continue
            raw_sha256 = verified_source.get("raw_sha256")
            if not isinstance(raw_sha256, str):
                _quarantine(admission, "openfootball_provenance_missing")
                continue
            admitted.append(
                {
                    "fixture_id": verified["id"],
                    "competition_id": verified["competition_id"],
                    "kickoff_at": kickoff,
                    "observed_at": verified_source_time,
                    "home_team": verified["home_team"],
                    "away_team": verified["away_team"],
                    "home_team_id": team_id(
                        str(verified["competition_id"]), str(verified["home_team"])
                    ),
                    "away_team_id": team_id(
                        str(verified["competition_id"]), str(verified["away_team"])
                    ),
                    "score": {"home": int(score["home"]), "away": int(score["away"])},
                    "halftime_score": verified.get("halftime_score"),
                    "source_file": str(
                        openfootball_raw_archive_dir
                        / "raw"
                        / "sha256"
                        / raw_sha256[:2]
                        / f"{raw_sha256}.raw"
                    ),
                    "source_sha256": raw_sha256,
                    "provider_fixture_id": str(verified["id"]),
                    "source_name": "OpenFootball",
                    "source_license_status": "CC0-1.0",
                    "kickoff_time_quality": str(verified.get("kickoff_time_quality") or "exact"),
                    "kickoff_time_source": str(verified.get("kickoff_time_source") or "explicit"),
                }
            )
            admission["admitted_rows"] += 1
    return admitted, admission, False


def _admission_payload(admission: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshots_checked": int(admission["snapshots_checked"]),
        "candidate_rows": int(admission["candidate_rows"]),
        "admitted_rows": int(admission["admitted_rows"]),
        "quarantined_rows": int(admission["quarantined_rows"]),
        "excluded_display_only_rows": int(admission["excluded_display_only_rows"]),
        "verified_archive_replays": int(admission["verified_archive_replays"]),
        "quarantine_reasons": dict(sorted(admission["quarantine_reasons"].items())),
        "exclusion_reasons": dict(sorted(admission["exclusion_reasons"].items())),
    }


def load_verified_result_rows(
    *,
    snapshot_paths: list[Path],
    openfootball_raw_archive_dir: Path,
    reference: datetime,
) -> dict[str, Any]:
    """Replay and admit only rights-cleared OpenFootball result observations."""

    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("reference must be timezone-aware")
    reference_utc = reference.astimezone(timezone.utc)
    rows: list[dict[str, Any]] = []
    admission = _empty_admission()
    invalid_snapshots = 0
    for path in snapshot_paths:
        path_rows, path_admission, invalid = _verified_candidate(
            path,
            reference=reference_utc,
            openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        )
        rows.extend(path_rows)
        _merge_admission(admission, path_admission)
        invalid_snapshots += int(invalid)
    return {
        "rows": rows,
        "admission": _admission_payload(admission),
        "invalid_snapshots": invalid_snapshots,
        "reference": reference_utc.isoformat(),
    }


def _valid_halftime_score(row: dict[str, Any]) -> dict[str, int] | None:
    """Return a normalized halftime score, ignoring absent/invalid metadata."""

    value = row.get("halftime_score")
    if not isinstance(value, dict):
        return None
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or int(value[key]) < 0
        for key in ("home", "away")
    ):
        return None
    final_score = row.get("score")
    if not isinstance(final_score, dict) or any(
        isinstance(final_score.get(key), bool)
        or not isinstance(final_score.get(key), int)
        or int(final_score[key]) < 0
        or int(value[key]) > int(final_score[key])
        for key in ("home", "away")
    ):
        # A half-time score cannot exceed the corresponding final score.  Do
        # not let malformed provider metadata alter the half/full target.
        return None
    return {"home": int(value["home"]), "away": int(value["away"])}


def _to_match(row: dict[str, Any]) -> Match:
    halftime_score = _valid_halftime_score(row)
    return Match(
        id=str(row["fixture_id"]),
        competition_id=str(row["competition_id"]),
        season=str(row["kickoff_at"].year),
        kickoff_at=row["kickoff_at"],
        home_team=str(row["home_team"]),
        away_team=str(row["away_team"]),
        home_team_id=str(row["home_team_id"]),
        away_team_id=str(row["away_team_id"]),
        status="finished",
        score=Score(
            home=int(row["score"]["home"]),
            away=int(row["score"]["away"]),
            halftime_home=(halftime_score["home"] if halftime_score is not None else None),
            halftime_away=(halftime_score["away"] if halftime_score is not None else None),
        ),
        market_probability=None,
        source_file=str(row["source_file"]),
        source_sha256=str(row["source_sha256"]),
        provider_fixture_id=str(row["provider_fixture_id"]),
        source_name=str(row["source_name"]),
        source_license_status=str(row.get("source_license_status") or "unknown"),
        kickoff_time_quality=(
            "date_only" if row.get("kickoff_time_quality") == "date_only" else "exact"
        ),
        kickoff_time_source=str(row.get("kickoff_time_source") or "provider"),
        kickoff_time_observed_at=row["observed_at"].isoformat(),
        result_observed_at=row["observed_at"].isoformat(),
    )


def load_live_results(
    *,
    live_path: Path,
    archive_dir: Path,
    openfootball_raw_archive_dir: Path,
    reference: datetime,
) -> dict[str, object]:
    """Return deduplicated, causally observed canonical results up to ``reference``."""

    replay = load_verified_result_rows(
        snapshot_paths=_paths(live_path, archive_dir),
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        reference=reference,
    )
    reference_utc = reference.astimezone(timezone.utc)
    by_fixture: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in replay["rows"]:
        by_fixture[str(row["fixture_id"])].append(row)

    conflicts: list[dict[str, object]] = []
    matches: list[Match] = []
    for fixture_id, rows in by_fixture.items():
        fixture_identities = {
            (
                row["competition_id"],
                row["kickoff_at"].isoformat(),
                row["home_team_id"],
                row["away_team_id"],
            )
            for row in rows
        }
        final_scores = {(row["score"]["home"], row["score"]["away"]) for row in rows}
        halftime_scores = {
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            for value in (_valid_halftime_score(row) for row in rows)
            if value is not None
        }
        if len(fixture_identities) > 1 or len(final_scores) > 1 or len(halftime_scores) > 1:
            conflicts.append(
                {
                    "fixture_id": fixture_id,
                    "reason": (
                        "fixture_identity_conflict"
                        if len(fixture_identities) > 1
                        else "score_conflict"
                    ),
                    "observations": sorted(
                        [
                            {
                                "observed_at": row["observed_at"].isoformat(),
                                "source_file": row["source_file"],
                                "competition_id": row["competition_id"],
                                "kickoff_at": row["kickoff_at"].isoformat(),
                                "home_team_id": row["home_team_id"],
                                "away_team_id": row["away_team_id"],
                                "score": row["score"],
                                "halftime_score": _valid_halftime_score(row),
                            }
                            for row in rows
                        ],
                        key=lambda value: (value["observed_at"], value["source_file"]),
                    ),
                }
            )
            continue
        # The newest observation carries the most recent source identity, but
        # identical scorelines remain a single immutable training event. A
        # later snapshot may omit halftime metadata that an earlier snapshot
        # supplied; merge that metadata instead of creating a false conflict.
        selected = max(rows, key=lambda row: (row["observed_at"], row["source_file"]))
        selected_halftime = _valid_halftime_score(selected)
        if selected_halftime is None and halftime_scores:
            selected = dict(selected)
            selected["halftime_score"] = json.loads(next(iter(halftime_scores)))
        matches.append(_to_match(selected))
    matches.sort(key=lambda match: (match.kickoff_at, match.id))
    return {
        "matches": matches,
        "diagnostics": {
            "candidate_rows": replay["admission"]["candidate_rows"],
            "deduplicated_matches": len(matches),
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "invalid_snapshots": replay["invalid_snapshots"],
            "reference": reference_utc.isoformat(),
            "admission": replay["admission"],
        },
    }


__all__ = ["load_live_results", "load_verified_result_rows"]
