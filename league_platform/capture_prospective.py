"""Capture causally frozen future predictions into the append-only archive."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from league_platform.current import attach_current_data
from league_platform.domain import Match
from league_platform.fixture_feed import fixture_rows
from league_platform.prospective_archive import DEFAULT_OUTPUT, append_predictions, validate_lock_integrity
from league_platform.prospective_evaluation import (
    require_durable_openfootball_raw_archive,
    resolve_openfootball_raw_archive_dir,
)
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_HISTORY_SOURCE_IDS
from league_platform.sources.live_results import load_live_results
from league_platform.sources.openfootball_verified import VerifiedOpenFootballHistorySource
from league_platform.strict_backtest import build_strict_prospective_predictions


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("live snapshot as_of must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _load_lock(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("prospective lock root must be an object")
    validate_lock_integrity(value)
    return value


def _evaluation_window_start(lock: dict) -> datetime | None:
    value = lock.get("evaluation_window_started_at")
    if value is None:
        return None
    return _utc(str(value))


def _blocked_diagnostics(blocked: object) -> dict[str, object]:
    """Keep structured, JSON-safe reasons for fixtures not yet capturable."""

    items: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    if isinstance(blocked, list):
        for value in blocked:
            if isinstance(value, dict):
                item: dict[str, object] = {}
                for key, raw in value.items():
                    if isinstance(raw, (str, int, float, bool)) or raw is None:
                        item[str(key)] = raw
                    elif isinstance(raw, (list, dict)):
                        # Block records are diagnostic input, not model data;
                        # preserve nested JSON while preventing a malformed
                        # provider object from breaking cycle evidence.
                        try:
                            json.dumps(raw, ensure_ascii=False)
                        except (TypeError, ValueError):
                            item[str(key)] = str(raw)
                        else:
                            item[str(key)] = raw
                item.setdefault("reason", "unknown")
            else:
                item = {"reason": str(value)}
            reason = str(item.get("reason") or "unknown")
            item["reason"] = reason
            counts[reason] += 1
            items.append(item)
    return {
        "count": len(items),
        "by_reason": dict(sorted(counts.items())),
        "items": items,
    }


def _current_match_to_domain(match: dict, *, live_path: Path) -> Match:
    raw_source = match.get("source")
    source = raw_source if isinstance(raw_source, dict) else {}
    raw_hash = source.get("raw_sha256")
    if not isinstance(raw_hash, str) or len(raw_hash) != 64:
        raise ValueError(f"current fixture {match.get('id')} has no valid raw hash")
    source_name = str(source.get("name") or "unknown")
    source_license = str(source.get("license") or source.get("license_status") or "unknown")
    return Match(
        id=str(match["id"]),
        competition_id=str(match["competition_id"]),
        season=str(match.get("season") or ""),
        kickoff_at=_utc(str(match["kickoff_at"])),
        home_team=str(match["home_team"]),
        away_team=str(match["away_team"]),
        home_team_id=str(match["home_team_id"]),
        away_team_id=str(match["away_team_id"]),
        status=str(match.get("status") or "upcoming"),
        score=None,
        market_probability=None,
        source_file=str(live_path),
        source_sha256=raw_hash,
        provider_fixture_id=str(
            match.get("native_fixture_id")
            or source.get("native_fixture_id")
            or match["id"]
        ),
        source_name=source_name,
        source_license_status=source_license,
        kickoff_time_quality=str(match.get("kickoff_time_quality") or "exact"),
        kickoff_time_source=str(match.get("kickoff_time_source") or source_name),
        kickoff_time_observed_at=(
            match.get("kickoff_time_observed_at") or source.get("retrieved_at")
        ),
    )


def _fixture_schedule_snapshot(fixture: dict) -> dict[str, str] | None:
    """Keep the exact schedule fields that existed in one archived snapshot."""

    kickoff_at = fixture.get("kickoff_at")
    if not isinstance(kickoff_at, str):
        return None
    try:
        _utc(kickoff_at)
    except ValueError:
        return None
    source = fixture.get("source")
    source_observed_at = (
        source.get("retrieved_at") if isinstance(source, dict) else None
    )
    observed_at = fixture.get("kickoff_time_observed_at") or source_observed_at
    if not isinstance(observed_at, str):
        return None
    try:
        _utc(observed_at)
    except ValueError:
        return None
    return {
        "kickoff_at": kickoff_at,
        "kickoff_time_quality": str(
            fixture.get("kickoff_time_quality") or "exact"
        ),
        "kickoff_time_source": str(
            fixture.get("kickoff_time_source")
            or (source.get("name") if isinstance(source, dict) else "provider")
        ),
        "kickoff_time_observed_at": observed_at,
    }


def _canonical_fixture_ids(live: dict) -> set[str]:
    """Return only IDs from the selected canonical fixture feed.

    This separates provider-neutral schedule rows from display-only lottery
    rows that ``attach_current_data`` may append to the workbench.
    """

    return {
        str(row["id"])
        for row in fixture_rows(live)
        if isinstance(row.get("id"), str) and row["id"]
    }


_FREEZE_OFFSETS = {
    "t_minus_24h": timedelta(hours=24),
    "t_minus_6h": timedelta(hours=6),
    "t_minus_90m": timedelta(minutes=90),
}
_PREDICTION_HORIZON = timedelta(days=7)


def _within_prediction_horizon(kickoff_at: datetime, *, as_of: datetime) -> bool:
    """Keep the operational freeze queue to the product's next-seven-day scope."""

    return as_of < kickoff_at <= as_of + _PREDICTION_HORIZON

# A long-running live archive can contain hundreds of complete source
# snapshots.  Re-validating every historical payload on every 15-minute poll
# is both wasteful and capable of exhausting the cycle deadline.  For a large
# archive we replay only the newest candidate at each due freeze cutoff (plus
# the newest pointer for lineup discovery).  Small archives retain the exact
# all-candidate behaviour used by tests and backfills.
_MAX_FULL_ARCHIVE_REPLAY_CANDIDATES = 128


def _snapshot_as_of(path: Path) -> datetime:
    """Read only the archive clock, rejecting malformed or naive snapshots."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return _utc(str(payload["as_of"]))


_LINEUP_PROVIDER_KEYS = ("official_lineup", "fotmob_lineup", "team_status")


def _has_complete_bilateral_starting_xi(provider: dict, provider_key: str) -> bool:
    """Require the confirming provider itself to carry exactly 11 starters per side."""

    lineups = provider.get("lineups")
    teams = (
        lineups
        if isinstance(lineups, dict)
        else provider.get("teams")
        if provider_key == "team_status"
        else None
    )
    if not isinstance(teams, dict):
        return False
    for side in ("home", "away"):
        side_payload = teams.get(side)
        players = side_payload.get("players") if isinstance(side_payload, dict) else None
        if not isinstance(players, list):
            return False
        starter_count = sum(
            1
            for player in players
            if isinstance(player, dict) and player.get("starter") is True
        )
        if starter_count != 11:
            return False
    return True


def _confirmed_lineup_event(
    features: object,
    *,
    kickoff_at: datetime,
    reference: datetime,
) -> tuple[datetime, str, dict] | None:
    """Return the newest causal lineup event and its exact provider payload."""

    if not isinstance(features, dict):
        return None
    candidates: list[tuple[datetime, int, str, dict]] = []
    # Official club/league data remains preferred.  Public-provider fallbacks
    # are accepted only when their adapter has already proved a complete XI
    # and set model_eligible=True under the same pre-kickoff boundary.
    for provider_priority, key in enumerate(_LINEUP_PROVIDER_KEYS):
        row = features.get(key)
        if not isinstance(row, dict):
            continue
        lineups = row.get("lineups")
        source = row.get("source")
        confirmed = (
            lineups.get("confirmed") is True
            if isinstance(lineups, dict)
            else row.get("confirmed") is True
            if key == "team_status"
            else False
        )
        model_eligible = (
            lineups.get("model_eligible") is True
            if isinstance(lineups, dict)
            else row.get("model_eligible") is True
            if key == "team_status"
            else False
        )
        if (
            not confirmed
            or not model_eligible
            or not _has_complete_bilateral_starting_xi(row, key)
        ):
            continue
        # The ESPN parser keeps the observation clock on the normalized row;
        # some older snapshots also copied it into ``source``.  Accept both
        # representations, but never fall back to the current process time.
        observed = source.get("retrieved_at") if isinstance(source, dict) else None
        if not isinstance(observed, str):
            observed = row.get("retrieved_at")
        if not isinstance(observed, str):
            continue
        try:
            observed_at = _utc(observed)
        except ValueError:
            continue
        if observed_at < kickoff_at and observed_at <= reference:
            candidates.append((observed_at, provider_priority, key, row))
    if not candidates:
        return None
    observed_at, _priority, provider_key, provider = max(
        candidates,
        key=lambda item: (item[0], -item[1]),
    )
    return observed_at, provider_key, provider


def _confirmed_lineup_observed_at(
    features: object,
    *,
    kickoff_at: datetime,
    reference: datetime,
) -> datetime | None:
    """Return the newest causal confirmed-lineup observation clock."""

    event = _confirmed_lineup_event(
        features,
        kickoff_at=kickoff_at,
        reference=reference,
    )
    return event[0] if event is not None else None


def _archive_candidates(
    *,
    live_path: Path,
    archive_dir: Path,
    reference: datetime,
) -> list[tuple[datetime, Path]]:
    """Return accepted live snapshots in observation order.

    The current pointer is included even when its archive manifest has not
    been flushed yet.  Invalid historical files are left for the diagnostics
    returned by ``_select_archived_features`` rather than blocking capture.
    """

    paths = [live_path]
    raw_dir = archive_dir / "raw"
    manifest_path = archive_dir / "snapshots.jsonl"
    manifest_candidates: list[tuple[datetime, Path]] = []
    if manifest_path.is_file():
        # The append-only manifest is a compact index over immutable raw
        # snapshots.  Use it to avoid parsing every 1MB payload merely to
        # discover its as_of clock.  The payload is still fully validated when
        # it is actually replayed below.
        try:
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    raw_path = record.get("raw_path") if isinstance(record, dict) else None
                    if not isinstance(raw_path, str) or not raw_path:
                        continue
                    relative = Path(raw_path)
                    if relative.is_absolute() or ".." in relative.parts:
                        continue
                    candidate = (archive_dir / relative).resolve()
                    root = archive_dir.resolve()
                    try:
                        candidate.relative_to(root)
                    except ValueError:
                        continue
                    as_of = _utc(str(record["as_of"]))
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if candidate.is_file() and as_of <= reference:
                    manifest_candidates.append((as_of, candidate))
        except OSError:
            manifest_candidates = []
    if manifest_candidates:
        paths.extend(path for _as_of, path in manifest_candidates)
    elif raw_dir.exists():
        # Compatibility fallback for test fixtures and pre-manifest archives.
        paths.extend(sorted(raw_dir.glob("*.json")))
    candidates: list[tuple[datetime, Path]] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            as_of = _snapshot_as_of(path)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if as_of <= reference:
            candidates.append((as_of, path))
    # If two files have the same as_of, prefer the live pointer because it is
    # the source that produced the current application state.
    candidates.sort(key=lambda item: (item[0], item[1].resolve() != live_path.resolve()))
    return candidates


def _prune_replay_candidates(
    candidates: list[tuple[datetime, Path]],
    upcoming: list[Match],
    *,
    reference: datetime,
) -> tuple[list[tuple[datetime, Path]], bool]:
    """Select only candidates that can win a due freeze stage.

    ``_select_archived_features`` is an as-of replay: for a given cutoff the
    newest snapshot observed no later than that cutoff wins.  Therefore older
    snapshots between two distinct cutoffs cannot change any result.  Keep the
    latest predecessor for each due cutoff and the latest snapshot itself for
    lineup discovery.  If the archive is small, return it unchanged so the
    historical replay path remains exhaustive.
    """

    if len(candidates) <= _MAX_FULL_ARCHIVE_REPLAY_CANDIDATES:
        return candidates, False
    cutoffs = sorted(
        {
            match.kickoff_at.astimezone(reference.tzinfo) - offset
            for match in upcoming
            for offset in _FREEZE_OFFSETS.values()
            if match.kickoff_at.astimezone(reference.tzinfo) - offset <= reference
        }
    )
    selected: dict[Path, tuple[datetime, Path]] = {}
    for cutoff in cutoffs:
        predecessor = next(
            (candidate for candidate in reversed(candidates) if candidate[0] <= cutoff),
            None,
        )
        if predecessor is not None:
            selected[predecessor[1].resolve()] = predecessor
    if candidates:
        latest = candidates[-1]
        selected[latest[1].resolve()] = latest
    return sorted(selected.values(), key=lambda item: (item[0], str(item[1]))), True


def _compact_platform_base(base: dict) -> dict:
    """Keep only the platform fields needed to join current fixtures.

    ``build_platform_snapshot`` also carries the complete historical match
    payload for the Sites page.  Prospective capture loads that history again
    as domain objects, so retaining or deep-copying the rendered history here
    needlessly multiplies the process peak.  The current-data validator only
    needs the competition identity space, summary container and match list.
    """

    return {
        "schema_version": base.get("schema_version"),
        "generated_at": base.get("generated_at"),
        "timezone": base.get("timezone"),
        "summary": {},
        "competitions": [
            {"id": item.get("id")}
            for item in base.get("competitions", [])
            if isinstance(item, dict) and item.get("id")
        ],
        "matches": [],
    }


def _select_archived_features(
    base: dict,
    upcoming: list[Match],
    *,
    live_path: Path,
    archive_dir: Path,
    reference: datetime,
) -> tuple[dict[str, dict], dict[str, datetime], dict[str, object]]:
    """Select the newest archived observation available at each freeze.

    Each attached snapshot is itself validated by ``attach_current_data``.
    A feature can only be selected when the complete source snapshot was
    observed no later than the stage cutoff; the strict renderer performs a
    second field-level timestamp check before blending it into probabilities.
    """

    wanted = {match.id: match for match in upcoming}
    selected: dict[str, dict] = {
        fixture_id: {"by_stage": {}}
        for fixture_id in wanted
    }
    lineup_times: dict[str, datetime] = {}
    lineup_events: dict[str, tuple[datetime, str, dict]] = {}
    diagnostics: dict[str, object] = {
        "candidate_count": 0,
        "attached_count": 0,
        "lineup_discovery_attached_count": 0,
        "lineup_replay_attached_count": 0,
        "lineup_event_overlay_count": 0,
        "selected_stage_features": 0,
        "errors": [],
    }
    # ``attach_current_data`` defensively deep-copies its snapshot input.  Use
    # a compact identity-only base so archived replay never copies the full
    # rendered historical match payload.
    feature_base = _compact_platform_base(base)
    candidates = _archive_candidates(
        live_path=live_path,
        archive_dir=archive_dir,
        reference=reference,
    )
    diagnostics["candidate_count"] = len(candidates)
    replay_candidates, pruned = _prune_replay_candidates(
        candidates,
        upcoming,
        reference=reference,
    )
    diagnostics["replayed_candidate_count"] = len(replay_candidates)
    diagnostics["candidate_pruned"] = pruned
    diagnostics["candidate_pruning_reason"] = (
        "latest_predecessor_per_due_cutoff_and_latest_lineup_pointer"
        if pruned
        else None
    )
    errors: list[dict[str, str]] = []
    attached_count = 0
    # First pass: discover the causal lineup observation clock and select the
    # three clock-based freeze stages.  A provider can publish a lineup with
    # an observation time slightly before the archive snapshot's ``as_of``
    # (the ESPN summary does this in practice), so the snapshot that first
    # reveals the lineup is not necessarily eligible to replay the lineup
    # stage itself.
    for as_of, path in replay_candidates:
        try:
            snapshot = attach_current_data(feature_base, path, now=as_of)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        attached_count += 1

        # Newer candidates win, but only among snapshots that existed by the
        # requested cutoff.  Candidates are ordered ascending, so applying
        # each snapshot immediately is deterministic and keeps only the
        # selected feature payload instead of retaining every full snapshot.
        fixtures_by_id = {
            str(item.get("id")): item
            for item in snapshot.get("matches", [])
            if isinstance(item, dict) and item.get("id") in wanted
        }
        for fixture_id, match in wanted.items():
            fixture = fixtures_by_id.get(fixture_id)
            if fixture is None:
                continue
            fixture_schedule = _fixture_schedule_snapshot(fixture)
            features = fixture.get("current_features")
            if isinstance(features, dict):
                event = _confirmed_lineup_event(
                    features, kickoff_at=match.kickoff_at, reference=reference
                )
                if event is not None:
                    existing = lineup_events.get(fixture_id)
                    event_priority = _LINEUP_PROVIDER_KEYS.index(event[1])
                    existing_priority = (
                        _LINEUP_PROVIDER_KEYS.index(existing[1])
                        if existing is not None
                        else len(_LINEUP_PROVIDER_KEYS)
                    )
                    if (
                        existing is None
                        or event[0] > existing[0]
                        or (event[0] == existing[0] and event_priority < existing_priority)
                    ):
                        lineup_events[fixture_id] = (
                            event[0],
                            event[1],
                            deepcopy(event[2]),
                        )
                        lineup_times[fixture_id] = event[0]
            for stage, offset in _FREEZE_OFFSETS.items():
                cutoff = match.kickoff_at.astimezone(reference.tzinfo) - offset
                # Do not preselect a stage whose cutoff itself is still in the
                # future.  It will be considered on the next capture after
                # that cutoff, using the archive available at that time.
                if cutoff > reference or as_of > cutoff or as_of > reference:
                    continue
                selected[fixture_id].setdefault("fixture_observed_at_by_stage", {})[
                    stage
                ] = as_of.isoformat()
                if fixture_schedule is not None:
                    selected[fixture_id].setdefault("fixture_by_stage", {})[
                        stage
                    ] = deepcopy(fixture_schedule)
                if isinstance(features, dict):
                    selected[fixture_id]["by_stage"][stage] = dict(features)
                    selected[fixture_id].setdefault("source_as_of_by_stage", {})[
                        stage
                    ] = as_of.isoformat()
        # ``snapshot`` can contain hundreds of current fixtures and provider
        # records.  Do not retain it across archive candidates; the selected
        # stage dictionaries above are the only replay state needed downstream.
        del snapshot
    diagnostics["attached_count"] = attached_count
    diagnostics["lineup_discovery_attached_count"] = attached_count

    # Second pass: once the observation clock is known, replay the newest
    # complete source snapshot that was itself observed no later than that
    # clock.  This pairs a pre-lineup feature snapshot with the independently
    # observed lineup event, while forbidding a later poll from leaking
    # post-confirmation information backward.
    lineup_replay_attached_count = 0
    lineup_event_overlay_fixture_ids: set[str] = set()
    if lineup_times:
        # A lineup observed in the newest pointer may have first appeared in
        # an earlier archive snapshot.  Add the exact predecessor of each
        # discovered observation clock before the causal replay pass.
        replay_by_path = {path.resolve(): (as_of, path) for as_of, path in replay_candidates}
        for lineup_at in lineup_times.values():
            predecessor = next(
                (candidate for candidate in reversed(candidates) if candidate[0] <= lineup_at),
                None,
            )
            if predecessor is not None:
                replay_by_path[predecessor[1].resolve()] = predecessor
        replay_candidates = sorted(
            replay_by_path.values(), key=lambda item: (item[0], str(item[1]))
        )
        for as_of, path in replay_candidates:
            eligible_fixtures = {
                fixture_id: match
                for fixture_id, match in wanted.items()
                if fixture_id in lineup_times
                and as_of <= lineup_times[fixture_id] < match.kickoff_at
            }
            if not eligible_fixtures:
                continue
            try:
                snapshot = attach_current_data(feature_base, path, now=as_of)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append({"path": str(path), "error": str(exc), "phase": "lineup_replay"})
                continue
            lineup_replay_attached_count += 1
            fixtures_by_id = {
                str(item.get("id")): item
                for item in snapshot.get("matches", [])
                if isinstance(item, dict) and item.get("id") in eligible_fixtures
            }
            for fixture_id in eligible_fixtures:
                fixture = fixtures_by_id.get(fixture_id)
                if not isinstance(fixture, dict):
                    continue
                selected[fixture_id].setdefault("fixture_observed_at_by_stage", {})[
                    "lineup_confirmation"
                ] = as_of.isoformat()
                fixture_schedule = _fixture_schedule_snapshot(fixture)
                if fixture_schedule is not None:
                    selected[fixture_id].setdefault("fixture_by_stage", {})[
                        "lineup_confirmation"
                    ] = deepcopy(fixture_schedule)
                features = fixture.get("current_features") if isinstance(fixture, dict) else None
                if not isinstance(features, dict):
                    continue
                stage_features = deepcopy(features)
                lineup_event = lineup_events.get(fixture_id)
                if lineup_event is not None:
                    observed_at, provider_key, provider = lineup_event
                    # Preserve the complete predecessor snapshot as the stage
                    # baseline, but overlay only the provider response whose
                    # own trusted retrieval clock established the lineup
                    # event.  No market/news/weather field from the later
                    # archive batch is borrowed.
                    stage_features[provider_key] = deepcopy(provider)
                    # Downstream fingerprints, player shadows and provenance
                    # must bind to this exact confirming response.  Other
                    # providers in the predecessor snapshot remain available
                    # for audit, but cannot silently replace the freeze event.
                    stage_features["lineup_confirmation_event"] = {
                        "observed_at": observed_at.isoformat(),
                        "provider_key": provider_key,
                    }
                    lineup_event_overlay_fixture_ids.add(fixture_id)
                selected[fixture_id]["by_stage"]["lineup_confirmation"] = stage_features
                selected[fixture_id].setdefault("source_as_of_by_stage", {})[
                    "lineup_confirmation"
                ] = as_of.isoformat()
            del snapshot
    # A provider response can be retrieved before the aggregate current
    # snapshot is atomically published.  When no whole-snapshot predecessor
    # exists at the lineup clock, keep only the exact complete XI response
    # whose own trusted retrieval time established the event.  Never borrow
    # market, news, weather, or other fields from the later aggregate poll.
    for fixture_id, (observed_at, provider_key, provider) in lineup_events.items():
        if fixture_id in lineup_event_overlay_fixture_ids:
            continue
        selected[fixture_id]["by_stage"]["lineup_confirmation"] = {
            provider_key: deepcopy(provider),
            "lineup_confirmation_event": {
                "observed_at": observed_at.isoformat(),
                "provider_key": provider_key,
            },
        }
        selected[fixture_id].setdefault("source_as_of_by_stage", {})[
            "lineup_confirmation"
        ] = observed_at.isoformat()
        lineup_event_overlay_fixture_ids.add(fixture_id)
    diagnostics["lineup_replay_attached_count"] = lineup_replay_attached_count
    diagnostics["lineup_event_overlay_count"] = len(lineup_event_overlay_fixture_ids)
    diagnostics["errors"] = errors
    diagnostics["selected_stage_features"] = sum(
        len(value.get("by_stage", {}))
        for value in selected.values()
        if isinstance(value, dict)
    )
    diagnostics["selected_fixture_observations"] = sum(
        len(value.get("fixture_observed_at_by_stage", {}))
        for value in selected.values()
        if isinstance(value, dict)
    )
    diagnostics["selected_fixture_schedules"] = sum(
        len(value.get("fixture_by_stage", {}))
        for value in selected.values()
        if isinstance(value, dict)
    )
    return selected, lineup_times, diagnostics


def capture(
    *,
    openfootball_raw_archive_dir: Path,
    data_dir: Path = Path("data/MatchHistory"),
    live_path: Path = Path("data/live/current.json"),
    archive_dir: Path = Path("data/live/archive"),
    lock_path: Path = Path("docs/evidence/prospective-model-lock-current.json"),
    output: Path = DEFAULT_OUTPUT,
) -> dict[str, object]:
    openfootball_raw_archive_dir = require_durable_openfootball_raw_archive(
        openfootball_raw_archive_dir
    )
    live = json.loads(live_path.read_text(encoding="utf-8"))
    as_of = _utc(str(live["as_of"]))
    canonical_fixture_ids = _canonical_fixture_ids(live)
    lock = _load_lock(lock_path)
    evaluation_window_start = _evaluation_window_start(lock)
    expected_competitions = live.get("expected_competitions")
    if not isinstance(expected_competitions, list) or any(
        not isinstance(competition_id, str) or not competition_id.strip()
        for competition_id in expected_competitions
    ):
        raise ValueError("live snapshot expected_competitions are invalid")
    # Formal prospective capture must not consult the legacy MatchHistory
    # directory.  Even its competition identity space comes from the current
    # handoff; historical domain rows are replayed below from verified raw
    # OpenFootball bytes at this exact cutoff.
    base = {
        "schema_version": "1.1.0",
        "generated_at": as_of.isoformat(),
        "timezone": "UTC",
        "summary": {},
        "competitions": [{"id": competition_id} for competition_id in expected_competitions],
        "matches": [],
    }
    snapshot = attach_current_data(base, live_path, now=as_of)
    history_source = VerifiedOpenFootballHistorySource(
        openfootball_raw_archive_dir,
        observed_before=as_of,
        source_ids=OPENFOOTBALL_HISTORY_SOURCE_IDS,
    )
    admission = history_source.admission_manifest()
    history: list[Match] = []
    competition_ids = admission.get("competition_ids")
    if not isinstance(competition_ids, list) or any(
        not isinstance(competition_id, str) or not competition_id.strip()
        for competition_id in competition_ids
    ):
        raise ValueError("verified OpenFootball history admission competitions are invalid")
    for competition_id in competition_ids:
        history.extend(history_source.load(competition_id).matches)
    live_results = load_live_results(
        live_path=live_path,
        archive_dir=archive_dir,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        reference=as_of,
    )
    live_result_matches = live_results.get("matches")
    if isinstance(live_result_matches, list):
        history.extend(match for match in live_result_matches if isinstance(match, Match))
    upcoming: list[Match] = []
    window_filtered = 0
    horizon_filtered = 0
    lineup_times: dict[str, datetime] = {}
    for fixture in snapshot["matches"]:
        if (
            fixture.get("status") != "upcoming"
            or str(fixture.get("id", "")) not in canonical_fixture_ids
        ):
            continue
        domain_fixture = _current_match_to_domain(fixture, live_path=live_path)
        if domain_fixture.kickoff_at <= as_of:
            continue
        if not _within_prediction_horizon(domain_fixture.kickoff_at, as_of=as_of):
            horizon_filtered += 1
            continue
        if (
            evaluation_window_start is not None
            and domain_fixture.kickoff_at < evaluation_window_start
        ):
            window_filtered += 1
            continue
        upcoming.append(domain_fixture)
        observed_at = _confirmed_lineup_observed_at(
            fixture.get("current_features"),
            kickoff_at=domain_fixture.kickoff_at,
            reference=as_of,
        )
        if observed_at is not None:
            lineup_times[domain_fixture.id] = observed_at
    feature_snapshots, archived_lineup_times, archive_diagnostics = _select_archived_features(
        base,
        upcoming,
        live_path=live_path,
        archive_dir=archive_dir,
        reference=as_of,
    )
    for fixture_id, observed_at in archived_lineup_times.items():
        if observed_at > lineup_times.get(
            fixture_id,
            datetime.min.replace(tzinfo=timezone.utc),
        ):
            lineup_times[fixture_id] = observed_at
    current_status = snapshot.get("current_data", {}).get("status")
    # The causal model only needs the domain history, upcoming fixtures and
    # selected feature payloads from this point onward.
    del snapshot, base
    result = build_strict_prospective_predictions(
        history,
        upcoming,
        as_of=as_of,
        lineup_observed_at=lineup_times,
        feature_snapshots=feature_snapshots,
        evaluation_window_started_at=evaluation_window_start,
    )
    blocked_diagnostics = _blocked_diagnostics(result.get("blocked", []))
    archive_result = append_predictions(
        output,
        list(result.get("predictions", [])),
        lock=lock,
        captured_at=as_of,
    )
    return {
        "as_of": as_of.isoformat(),
        "current_status": current_status,
        "upcoming_fixtures": len(upcoming),
        "window_filtered": window_filtered,
        "horizon_filtered": horizon_filtered,
        "lineup_observed": len(lineup_times),
        "predictions": len(result.get("predictions", [])),
        "blocked": blocked_diagnostics["count"],
        "blocked_diagnostics": blocked_diagnostics,
        "archive": str(output),
        "feature_archive": archive_diagnostics,
        "live_results": live_results.get("diagnostics", {}),
        **archive_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/MatchHistory"))
    parser.add_argument("--live-path", type=Path, default=Path("data/live/current.json"))
    parser.add_argument("--archive-dir", type=Path, default=Path("data/live/archive"))
    parser.add_argument("--openfootball-raw-archive-dir", type=Path)
    parser.add_argument(
        "--lock", type=Path, default=Path("docs/evidence/prospective-model-lock-current.json")
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    raw_archive_dir = resolve_openfootball_raw_archive_dir(
        args.openfootball_raw_archive_dir,
        runtime_default=args.archive_dir.parent / "openfootball-raw",
    )
    print(
        json.dumps(
            capture(
                data_dir=args.data_dir,
                live_path=args.live_path,
                lock_path=args.lock,
                output=args.output,
                archive_dir=args.archive_dir,
                openfootball_raw_archive_dir=raw_archive_dir,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = ["capture"]
