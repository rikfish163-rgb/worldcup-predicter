"""Capture a bounded live-only overlay without touching the forecast ledger.

The normal prospective cycle is intentionally a relatively heavy research
snapshot.  This module is a separate, display-only lane for currently live
fixtures.  It never writes predictions, outcomes or model features.  The
legacy ESPN implementation is retained for authorized fixtures/tests, but the
default production path fails closed before network access until an express
written provider authorization is configured in code.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse

from league_platform.fixture_feed import fixture_rows
from league_platform.live_sources.espn_market import fetch_espn_markets
from league_platform.live_sources.openligadb import (
    OPENLIGADB_HOST,
    OPENLIGADB_PATH,
    fetch_openligadb_live_updates,
)
from league_platform.identity import team_id


SCHEMA_VERSION = "matchline.live_overlay.v1"
DEFAULT_LIVE_PATH = Path("data/live/current.json")
DEFAULT_OUTPUT = Path("data/live/live_overlay.json")
DEFAULT_ARCHIVE_DIR = Path("data/live/archive/live_overlay")
MAX_FIXTURES = 80
MAX_EVENTS_PER_FIXTURE = 100
MAX_ERRORS = 40
DEFAULT_LOOKBACK_MINUTES = 180
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
ESPN_TERMS_URL = "https://disneytermsofuse.com/english/"


class RuntimeMutationBusyError(RuntimeError):
    """Raised when the prospective cycle or archive currently owns the runtime."""


@contextmanager
def _runtime_mutation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeMutationBusyError(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 240) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_text(value: Any) -> str | None:
    parsed = _iso(value)
    return parsed.isoformat() if parsed is not None else None


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read one canonical current snapshot, failing closed on malformed input."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("live snapshot must be an object")
    return payload


def select_live_fixtures(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> list[dict[str, Any]]:
    """Select only in-progress or recently-started fixtures from the snapshot.

    A stale ``upcoming`` status is included after its kickoff so a live-only
    poll can repair a lagging scoreboard.  Future fixtures and finished rows
    are excluded; an ambiguous or malformed kickoff is never guessed.
    """

    reference = _now(now)
    minimum = reference - timedelta(minutes=max(1, min(360, int(lookback_minutes))))
    fixtures = fixture_rows(snapshot)
    selected: list[dict[str, Any]] = []
    for raw in fixtures:
        if not isinstance(raw, dict):
            continue
        fixture_id = _text(raw.get("id"), 120)
        kickoff = _iso(raw.get("kickoff_at"))
        status = _text(raw.get("status"), 30)
        if fixture_id is None or kickoff is None or status not in {"live", "upcoming"}:
            continue
        if kickoff > reference or kickoff < minimum:
            continue
        source = _record(raw.get("source"))
        native_id = _text(source.get("native_fixture_id"), 80)
        competition_id = _text(raw.get("competition_id"), 80)
        if competition_id is None:
            continue
        source_identity: dict[str, Any] = {}
        if native_id is not None:
            source_identity["native_fixture_id"] = native_id
        source_name = _text(source.get("name"), 80)
        if source_name is not None:
            source_identity["name"] = source_name
        selected.append({
            "id": fixture_id,
            "competition_id": competition_id,
            "kickoff_at": kickoff.isoformat(),
            "status": status,
            "home_team": _text(raw.get("home_team"), 160),
            "away_team": _text(raw.get("away_team"), 160),
            "source": source_identity,
        })
    return sorted(selected, key=lambda item: (item["kickoff_at"], item["id"]))[:MAX_FIXTURES]


def _valid_openligadb_source(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    parsed = urlparse(str(value.get("url") or ""))
    raw_sha256 = value.get("raw_sha256")
    return (
        value.get("name") == "OpenLigaDB"
        and value.get("license") == "ODbL-1.0"
        and value.get("attribution_required") is True
        and parsed.scheme == "https"
        and parsed.hostname == OPENLIGADB_HOST
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and OPENLIGADB_PATH.fullmatch(parsed.path) is not None
        and not parsed.query
        and not parsed.fragment
        and isinstance(raw_sha256, str)
        and len(raw_sha256) == 64
        and all(character in "0123456789abcdef" for character in raw_sha256.lower())
    )


def select_openligadb_live_targets(
    snapshot: dict[str, Any],
    fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strictly join canonical Bundesliga rows to licensed provider IDs."""

    envelope = snapshot.get("openligadb")
    matches = envelope.get("matches") if isinstance(envelope, dict) else None
    if not isinstance(matches, list):
        return []
    index: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for match in matches:
        if not isinstance(match, dict) or match.get("competition_id") != "bundesliga":
            continue
        provider_match_id = _text(match.get("provider_match_id"), 20)
        kickoff = _iso_text(match.get("kickoff_at"))
        home = _text(match.get("home_team"), 160)
        away = _text(match.get("away_team"), 160)
        if (
            provider_match_id is None
            or not provider_match_id.isdigit()
            or kickoff is None
            or home is None
            or away is None
            or not _valid_openligadb_source(match.get("source"))
        ):
            continue
        key = ("bundesliga", kickoff, team_id("bundesliga", home), team_id("bundesliga", away))
        index.setdefault(key, []).append(match)
    targets: list[dict[str, Any]] = []
    for fixture in fixtures:
        competition_id = _text(fixture.get("competition_id"), 80)
        kickoff = _iso_text(fixture.get("kickoff_at"))
        home = _text(fixture.get("home_team"), 160)
        away = _text(fixture.get("away_team"), 160)
        if competition_id != "bundesliga" or kickoff is None or home is None or away is None:
            continue
        key = (competition_id, kickoff, team_id(competition_id, home), team_id(competition_id, away))
        candidates = index.get(key, [])
        if len(candidates) != 1:
            continue
        targets.append({
            **fixture,
            "provider": "OpenLigaDB",
            "provider_match_id": str(candidates[0]["provider_match_id"]),
        })
    return targets


def _latest_by_fixture(rows: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture_id = _text(row.get("fixture_id"), 120)
        if fixture_id is None:
            continue
        candidate_time = _iso(row.get("observed_at") or row.get("retrieved_at"))
        previous = result.get(fixture_id)
        previous_time = _iso(previous.get("observed_at") or previous.get("retrieved_at")) if previous else None
        if previous is None or (candidate_time is not None and (previous_time is None or candidate_time > previous_time)):
            result[fixture_id] = row
    return result


def _bounded_events(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    events = row.get("events") if isinstance(row, dict) else None
    return [event for event in events[:MAX_EVENTS_PER_FIXTURE] if isinstance(event, dict)] if isinstance(events, list) else []


def _storage_guard(
    paths: list[Path | None],
    *,
    min_free_bytes: int,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Check overlay write targets before reading the external snapshot.

    The overlay writes both to the external runtime and to the repository's
    static Sites asset.  If the repository filesystem is full, reading the
    external NTFS snapshot first can leave the process blocked in kernel I/O
    while the service timeout repeatedly kills it.  This preflight is
    intentionally read-only and returns a bounded diagnostic instead of
    touching the live snapshot or replacing the last valid overlay.
    """

    required = max(0, int(min_free_bytes))
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths:
        if raw_path is None:
            continue
        path = Path(raw_path)
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            resolved = str(target.resolve(strict=False))
        except OSError:
            resolved = str(target)
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            usage = disk_usage_fn(target)
            free_bytes = int(usage.free)
            checks.append({
                "path": str(path),
                "filesystem": resolved,
                "free_bytes": free_bytes,
                "required_free_bytes": required,
                "status": "ok" if free_bytes >= required else "blocked",
            })
        except OSError as exc:
            checks.append({
                "path": str(path),
                "filesystem": resolved,
                "free_bytes": None,
                "required_free_bytes": required,
                "status": "blocked",
                "reason": "path_unavailable",
                "error": type(exc).__name__,
            })
        # The repository/static target is checked first by poll_once.  Stop
        # on the first failure so a full root filesystem cannot be followed by
        # a potentially blocking stat/read on an unstable external mount.
        if checks[-1]["status"] != "ok":
            break
    blocked = [item for item in checks if item.get("status") != "ok"]
    return {
        "status": "blocked" if blocked else "ok",
        "required_free_bytes": required,
        "checked": checks,
        "blocked_paths": [item["path"] for item in blocked],
    }


def _blocked_storage_overlay(reference: datetime, storage: dict[str, Any]) -> dict[str, Any]:
    """Return a display-only failure without overwriting the last good file."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked_storage",
        "retrieved_at": reference.isoformat(),
        "as_of": reference.isoformat(),
        "fixture_count": 0,
        "freshness_budget_seconds": 90,
        "fixtures": [],
        "errors": ["insufficient_filesystem_space"],
        "storage": storage,
        "source": {
            "name": "ESPN event summary",
            "role": "live_display_only",
            "url_policy": "fixed_https_provider_hosts",
        },
        "policy": {
            "model_eligible": False,
            "results_and_events": "display_only",
            "pre_match_features": "never_written",
            "credentials_or_access_bypass": False,
        },
    }


def _rights_blocked_overlay(reference: datetime) -> dict[str, Any]:
    """Return the production default without opening the legacy provider."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rights_blocked",
        "retrieved_at": None,
        "checked_at": reference.isoformat(),
        "as_of": reference.isoformat(),
        "fixture_count": 0,
        "freshness_budget_seconds": 90,
        "fixtures": [],
        "errors": [],
        "source": {
            "name": "ESPN event summary",
            "role": "live_display_only",
            "terms_url": ESPN_TERMS_URL,
            "rights_status": "blocked_pending_express_written_permission",
        },
        "policy": {
            "model_eligible": False,
            "results_and_events": "unavailable_until_authorized",
            "pre_match_features": "never_written",
            "credentials_or_access_bypass": False,
            "authorization_required": "express_written_permission",
            "network_opened": False,
        },
    }


def project_live_overlay(
    fixtures: list[dict[str, Any]],
    market_payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project provider updates into a display-only, appendable overlay."""

    reference = _now(now)
    updates = _latest_by_fixture(market_payload.get("fixture_updates"))
    incidents = _latest_by_fixture(market_payload.get("incidents"))
    stats = _latest_by_fixture(market_payload.get("match_stats"))
    rows: list[dict[str, Any]] = []
    for fixture in fixtures:
        fixture_id = fixture["id"]
        update = updates.get(fixture_id, {})
        incident = incidents.get(fixture_id, {})
        stat = stats.get(fixture_id, {})
        observed_values = [
            _iso(update.get("observed_at")),
            _iso(incident.get("retrieved_at")),
            _iso(stat.get("retrieved_at")),
        ]
        observed = max((value for value in observed_values if value is not None), default=None)
        source = update.get("source") or incident.get("source") or stat.get("source") or {
            "name": "ESPN event summary",
        }
        rows.append({
            "fixture_id": fixture_id,
            "competition_id": fixture.get("competition_id"),
            "kickoff_at": fixture.get("kickoff_at"),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "status": _text(update.get("status"), 30) or fixture.get("status"),
            "score": update.get("score") if isinstance(update.get("score"), dict) else None,
            "halftime_score": update.get("halftime_score") if isinstance(update.get("halftime_score"), dict) else None,
            "result_scope": _text(update.get("result_scope"), 40),
            "clock": update.get("clock") if isinstance(update.get("clock"), (str, int, float)) else None,
            "period": update.get("period") if isinstance(update.get("period"), int) and not isinstance(update.get("period"), bool) else None,
            "status_text": _text(update.get("status_text"), 120),
            "events": _bounded_events(incident),
            "stats": stat if stat else None,
            "observed_at": observed.isoformat() if observed is not None else None,
            "source": source,
            "enters_model": False,
            "model_eligible": False,
            "model_exclusion_reason": "live_or_postmatch_event",
        })
    raw_errors = market_payload.get("errors")
    errors: list[Any] = raw_errors if isinstance(raw_errors, list) else []
    provider_source = market_payload.get("source") if isinstance(market_payload.get("source"), dict) else None
    source_summary = provider_source or {
        "name": "ESPN event summary",
        "role": "live_display_only",
        "url_policy": "fixed_https_provider_hosts",
    }
    policy = {
        "model_eligible": False,
        "results_and_events": "display_only",
        "pre_match_features": "never_written",
        "credentials_or_access_bypass": False,
    }
    if isinstance(market_payload.get("network_opened"), bool):
        policy["network_opened"] = market_payload["network_opened"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if rows and not errors else "partial" if rows else "no_live_fixtures",
        "retrieved_at": reference.isoformat(),
        "as_of": reference.isoformat(),
        "fixture_count": len(rows),
        "freshness_budget_seconds": 90,
        "fixtures": rows,
        "errors": [str(error)[:400] for error in errors[:MAX_ERRORS]],
        "source": source_summary,
        "policy": policy,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def poll_once(
    *,
    live_path: Path = DEFAULT_LIVE_PATH,
    output: Path = DEFAULT_OUTPUT,
    archive_dir: Path | None = DEFAULT_ARCHIVE_DIR,
    public_output: Path | None = None,
    served_output: Path | None = None,
    now: datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    fetcher: Callable[..., dict[str, Any]] = fetch_espn_markets,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    reference = _now(now)
    # Check the repository/static destinations before touching the external
    # snapshot.  The order is deliberate: a full root filesystem is the
    # cheapest and safest failure to report.
    storage = _storage_guard(
        [public_output, served_output, output, archive_dir],
        min_free_bytes=min_free_bytes,
        disk_usage_fn=disk_usage_fn,
    )
    if storage["status"] != "ok":
        return _blocked_storage_overlay(reference, storage)
    if fetcher is fetch_espn_markets:
        overlay = _rights_blocked_overlay(reference)
        _write_json_atomic(output, overlay)
        if public_output is not None:
            _write_json_atomic(public_output, overlay)
        if served_output is not None:
            _write_json_atomic(served_output, overlay)
        if archive_dir is not None:
            archive_name = reference.strftime("%Y%m%dT%H%M%SZ") + ".json"
            _write_json_atomic(archive_dir / archive_name, overlay)
        return overlay
    snapshot = load_snapshot(live_path)
    fixtures = select_live_fixtures(snapshot, now=reference, lookback_minutes=lookback_minutes)
    if fixtures:
        payload = fetcher(
            fixtures,
            now=reference,
            horizon_days=1,
            max_fixtures=len(fixtures),
            postmatch_days=0,
            max_postmatch_fixtures=0,
        )
        if not isinstance(payload, dict):
            payload = {"errors": ["live fetcher returned a non-object"]}
    else:
        payload = {"errors": []}
    overlay = project_live_overlay(fixtures, payload, now=reference)
    _write_json_atomic(output, overlay)
    # The public copy is an optional, explicitly configured read-model bridge.
    # It is never the source of truth and is intentionally separate from the
    # append-only runtime archive.  Sites can serve this asset without giving
    # the browser access to the external runtime directory.
    if public_output is not None:
        _write_json_atomic(public_output, overlay)
    if served_output is not None:
        # Optional local production-server copy.  Cloudflare/Sites deployments
        # still require an explicit build or D1 publication; this only keeps a
        # running local build from serving an older static asset.
        _write_json_atomic(served_output, overlay)
    if archive_dir is not None:
        archive_name = reference.strftime("%Y%m%dT%H%M%SZ") + ".json"
        _write_json_atomic(archive_dir / archive_name, overlay)
    return overlay


def poll_openligadb_once(
    *,
    live_path: Path = DEFAULT_LIVE_PATH,
    output: Path = DEFAULT_OUTPUT,
    archive_dir: Path | None = DEFAULT_ARCHIVE_DIR,
    public_output: Path | None = None,
    served_output: Path | None = None,
    now: datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    fetcher: Callable[..., dict[str, Any]] = fetch_openligadb_live_updates,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Explicit ODbL display lane; callers must opt in and choose outputs."""

    reference = _now(now)
    storage = _storage_guard(
        [public_output, served_output, output, archive_dir],
        min_free_bytes=min_free_bytes,
        disk_usage_fn=disk_usage_fn,
    )
    if storage["status"] != "ok":
        blocked = _blocked_storage_overlay(reference, storage)
        blocked["source"] = {
            "name": "OpenLigaDB",
            "role": "live_display_only",
            "license": "ODbL-1.0",
            "license_url": "https://www.openligadb.de/lizenz",
            "attribution_required": True,
            "share_alike_required": True,
        }
        return blocked
    snapshot = load_snapshot(live_path)
    fixtures = select_live_fixtures(snapshot, now=reference, lookback_minutes=lookback_minutes)
    targets = select_openligadb_live_targets(snapshot, fixtures)
    source = {
        "name": "OpenLigaDB",
        "role": "live_display_only",
        "license": "ODbL-1.0",
        "license_url": "https://www.openligadb.de/lizenz",
        "attribution_required": True,
        "share_alike_required": True,
    }
    if targets:
        payload = fetcher(targets, now=reference, max_matches=len(targets))
        if not isinstance(payload, dict):
            payload = {"source": source, "errors": ["OpenLigaDB live fetcher returned a non-object"]}
    else:
        payload = {"source": source, "errors": [], "network_opened": False}
    overlay = project_live_overlay(targets, payload, now=reference)
    overlay["join_diagnostics"] = {
        "candidate_fixture_count": len(fixtures),
        "joined_fixture_count": len(targets),
        "unjoined_fixture_count": len(fixtures) - len(targets),
        "join_contract": "competition_exact_kickoff_canonical_home_away",
    }
    if fixtures and not targets:
        overlay["status"] = "no_joined_live_fixtures"
        overlay["errors"] = ["openligadb_exact_join_unavailable"]
    _write_json_atomic(output, overlay)
    if public_output is not None:
        _write_json_atomic(public_output, overlay)
    if served_output is not None:
        _write_json_atomic(served_output, overlay)
    if archive_dir is not None:
        archive_name = reference.strftime("%Y%m%dT%H%M%SZ") + ".json"
        _write_json_atomic(archive_dir / archive_name, overlay)
    return overlay


def poll_openligadb_once_locked(
    *,
    live_path: Path = DEFAULT_LIVE_PATH,
    output: Path = DEFAULT_OUTPUT,
    archive_dir: Path | None = DEFAULT_ARCHIVE_DIR,
    public_output: Path | None = None,
    served_output: Path | None = None,
    lock_path: Path | None = None,
    now: datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    fetcher: Callable[..., dict[str, Any]] = fetch_openligadb_live_updates,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Run the explicit licensed live lane under the shared runtime lock."""

    runtime_lock = lock_path or (live_path.parent / "prospective-cycle.lock")
    try:
        with _runtime_mutation_lock(runtime_lock):
            return poll_openligadb_once(
                live_path=live_path,
                output=output,
                archive_dir=archive_dir,
                public_output=public_output,
                served_output=served_output,
                now=now,
                lookback_minutes=lookback_minutes,
                fetcher=fetcher,
                min_free_bytes=min_free_bytes,
                disk_usage_fn=disk_usage_fn,
            )
    except RuntimeMutationBusyError:
        reference = _now(now)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "busy",
            "retrieved_at": reference.isoformat(),
            "as_of": reference.isoformat(),
            "fixture_count": 0,
            "freshness_budget_seconds": 90,
            "fixtures": [],
            "errors": ["runtime_mutation_lock_busy"],
            "source": {
                "name": "OpenLigaDB",
                "role": "live_display_only",
                "license": "ODbL-1.0",
                "license_url": "https://www.openligadb.de/lizenz",
                "attribution_required": True,
                "share_alike_required": True,
            },
            "policy": {
                "model_eligible": False,
                "results_and_events": "display_only",
                "pre_match_features": "never_written",
                "credentials_or_access_bypass": False,
                "network_opened": False,
            },
        }


def poll_once_locked(
    *,
    live_path: Path = DEFAULT_LIVE_PATH,
    output: Path = DEFAULT_OUTPUT,
    archive_dir: Path | None = DEFAULT_ARCHIVE_DIR,
    public_output: Path | None = None,
    served_output: Path | None = None,
    lock_path: Path | None = None,
    now: datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    fetcher: Callable[..., dict[str, Any]] = fetch_espn_markets,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    """Poll only when no prospective cycle or archive is mutating the runtime."""

    runtime_lock = lock_path or (live_path.parent / "prospective-cycle.lock")
    try:
        with _runtime_mutation_lock(runtime_lock):
            return poll_once(
                live_path=live_path,
                output=output,
                archive_dir=archive_dir,
                public_output=public_output,
                served_output=served_output,
                now=now,
                lookback_minutes=lookback_minutes,
                fetcher=fetcher,
                min_free_bytes=min_free_bytes,
                disk_usage_fn=disk_usage_fn,
            )
    except RuntimeMutationBusyError:
        reference = _now(now)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "busy",
            "retrieved_at": reference.isoformat(),
            "as_of": reference.isoformat(),
            "fixture_count": 0,
            "freshness_budget_seconds": 90,
            "fixtures": [],
            "errors": ["runtime_mutation_lock_busy"],
            "source": {
                "name": "ESPN event summary",
                "role": "live_display_only",
                "url_policy": "fixed_https_provider_hosts",
            },
            "policy": {
                "model_eligible": False,
                "results_and_events": "display_only",
                "pre_match_features": "never_written",
                "credentials_or_access_bypass": False,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-path", type=Path, default=DEFAULT_LIVE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--public-output",
        type=Path,
        default=None,
        help="optional static Sites read-model copy; remains display-only",
    )
    parser.add_argument(
        "--served-output",
        type=Path,
        default=None,
        help="optional local built-client copy for an already running Sites server",
    )
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--lookback-minutes", type=int, default=DEFAULT_LOOKBACK_MINUTES)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument(
        "--provider",
        choices=("rights-blocked-espn", "openligadb"),
        default="rights-blocked-espn",
        help=(
            "default remains a no-network rights block; OpenLigaDB is an "
            "explicit ODbL display-only opt-in"
        ),
    )
    args = parser.parse_args()
    poller = (
        poll_openligadb_once_locked
        if args.provider == "openligadb"
        else poll_once_locked
    )
    result = poller(
        live_path=args.live_path,
        output=args.output,
        public_output=args.public_output,
        served_output=args.served_output,
        archive_dir=args.archive_dir,
        lookback_minutes=args.lookback_minutes,
        min_free_bytes=args.min_free_bytes,
    )
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "blocked_storage":
        raise SystemExit(2)


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_MIN_FREE_BYTES",
    "SCHEMA_VERSION",
    "load_snapshot",
    "poll_openligadb_once",
    "poll_openligadb_once_locked",
    "poll_once",
    "poll_once_locked",
    "project_live_overlay",
    "select_openligadb_live_targets",
    "select_live_fixtures",
]
