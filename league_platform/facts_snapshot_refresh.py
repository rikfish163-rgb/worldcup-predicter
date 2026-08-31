"""Refresh the current facts snapshot without entering the model lane.

This producer deliberately wraps :func:`league_platform.sync_live.sync` rather
than the prospective cycle.  It can therefore keep the public schedule fresh
when a model lock is stale or a prospective window is correctly fail-closed.
The current snapshot is still written atomically by ``sync_live``; raw
OpenFootball evidence remains on the durable archive and downstream Sites
uploaders can publish only their bounded facts projection.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from league_platform.fixture_feed import fixture_rows
from league_platform.openfootball_history_sync import _validate_durable_raw_root
from league_platform.sync_live import (
    DEFAULT_ARCHIVE_DIR,
    DEFAULT_INTELLIGENCE_LEDGER,
    DEFAULT_OUTPUT,
    sync,
)


FACTS_REFRESH_SCHEMA = "matchline.facts_snapshot_refresh.v1"
RUNTIME_DIR_ENV = "MATCHLINE_RUNTIME_DIR"
OPENFOOTBALL_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
FACTS_CYCLE_LOCK_ENV = "MATCHLINE_FACTS_CYCLE_LOCK"

SyncFunction = Callable[..., dict[str, Any]]


def _default_runtime_dir() -> Path:
    configured = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_OUTPUT.parent


def _default_raw_archive() -> Path:
    configured = os.environ.get(OPENFOOTBALL_RAW_ARCHIVE_ENV, "").strip()
    return Path(configured) if configured else _default_runtime_dir() / "openfootball-raw"


def _default_cycle_lock() -> Path:
    configured = os.environ.get(FACTS_CYCLE_LOCK_ENV, "").strip()
    return Path(configured) if configured else _default_runtime_dir() / "prospective-cycle.lock"


@contextmanager
def _exclusive_cycle_lock(path: Path) -> Iterator[None]:
    """Serialize facts refreshes with the prospective cycle, fail closed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Matchline facts or prospective cycle is running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _count_status(rows: list[dict[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row.get("status") == status)


def _provider_statuses(snapshot: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for key, value in snapshot.items():
        if not isinstance(value, dict):
            continue
        status = value.get("status")
        if isinstance(status, str) and status.strip():
            statuses[key] = status.strip()[:80]
    return dict(sorted(statuses.items()))


def _summary(snapshot: dict[str, Any], *, output: Path, raw_archive: Path) -> dict[str, Any]:
    rows = fixture_rows(snapshot)
    return {
        "schema_version": FACTS_REFRESH_SCHEMA,
        "status": "ok",
        "lane": "facts_only",
        "output": str(output),
        "raw_archive": str(raw_archive),
        "as_of": snapshot.get("as_of"),
        "fixtures": len(rows),
        "upcoming": _count_status(rows, "upcoming"),
        "live": _count_status(rows, "live"),
        "finished": _count_status(rows, "finished"),
        "provider_statuses": _provider_statuses(snapshot),
        "model_lane": "not_run",
        "publication": "facts_sync_required",
    }


def refresh(
    output: Path = DEFAULT_OUTPUT,
    *,
    archive_dir: Path | None = None,
    openfootball_raw_archive_dir: Path | None = None,
    intelligence_ledger: Path | None = DEFAULT_INTELLIGENCE_LEDGER,
    cycle_lock_path: Path | None = None,
    crawl4ai_config_path: Path | str | None = None,
    allow_empty_snapshot: bool = False,
    enable_wikidata: bool = True,
    now: datetime | None = None,
    sync_fn: SyncFunction = sync,
) -> dict[str, Any]:
    """Refresh facts and return a bounded, non-model summary.

    The durable raw root is validated before taking the shared cycle lock or
    opening any network-capable adapter.  ``sync_fn`` is injectable for unit
    tests; production callers should use the default ``sync`` implementation.
    """

    output = Path(output)
    raw_archive = Path(openfootball_raw_archive_dir or _default_raw_archive())
    # The history producer owns the canonical no-symlink/no-tmpfs path policy.
    # Reuse that policy so a facts refresh cannot silently write evidence to a
    # volatile or operator-ambiguous location.
    raw_archive = _validate_durable_raw_root(raw_archive)
    if not raw_archive.is_dir() or not (raw_archive / "manifest.jsonl").is_file():
        raise ValueError("OpenFootball raw archive directory or manifest is missing")
    lock_path = Path(cycle_lock_path or _default_cycle_lock())
    selected_archive_dir = Path(archive_dir) if archive_dir is not None else output.parent / DEFAULT_ARCHIVE_DIR.name

    with _exclusive_cycle_lock(lock_path):
        snapshot = sync_fn(
            output,
            now=now,
            archive_dir=selected_archive_dir,
            openfootball_raw_archive_dir=raw_archive,
            intelligence_ledger=intelligence_ledger,
            crawl4ai_config_path=crawl4ai_config_path,
            allow_empty_snapshot=allow_empty_snapshot,
            enable_wikidata=enable_wikidata,
        )
    if not isinstance(snapshot, dict):
        raise TypeError("facts sync returned a non-object snapshot")
    return _summary(snapshot, output=output, raw_archive=raw_archive)


def main() -> None:
    runtime_dir = _default_runtime_dir()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=runtime_dir / "current.json")
    parser.add_argument("--archive-dir", type=Path, default=runtime_dir / DEFAULT_ARCHIVE_DIR.name)
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        default=None,
        help=(
            "durable OpenFootball raw root; defaults to "
            f"{OPENFOOTBALL_RAW_ARCHIVE_ENV} or the runtime root"
        ),
    )
    parser.add_argument("--intelligence-ledger", type=Path, default=runtime_dir / "intelligence" / "observations.jsonl")
    parser.add_argument("--cycle-lock", type=Path, default=None)
    parser.add_argument("--crawl4ai-config", type=Path, default=None)
    parser.add_argument("--allow-empty-snapshot", action="store_true")
    parser.add_argument("--disable-wikidata", action="store_true")
    args = parser.parse_args()
    result = refresh(
        output=args.output,
        archive_dir=args.archive_dir,
        openfootball_raw_archive_dir=args.openfootball_raw_archive_dir,
        intelligence_ledger=args.intelligence_ledger,
        cycle_lock_path=args.cycle_lock,
        crawl4ai_config_path=args.crawl4ai_config,
        allow_empty_snapshot=args.allow_empty_snapshot,
        enable_wikidata=not args.disable_wikidata,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["FACTS_REFRESH_SCHEMA", "refresh"]
