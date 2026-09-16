"""Resolve append-only runtime paths without changing the project layout.

The project keeps model locks, reports and source code in the repository, but
the prospective cycle writes growing snapshots, raw pages and ledgers.  This
small resolver lets an operator place only those runtime artifacts on a
separate filesystem while preserving the existing default paths and the
storage guard.  It never creates, moves or deletes files by itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RUNTIME_DIR = Path("data/live")
OPENFOOTBALL_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"


@dataclass(frozen=True)
class RuntimePaths:
    """Paths that grow during scheduled data production."""

    root: Path
    live_path: Path
    archive_dir: Path
    intelligence_ledger: Path
    intelligence_quarantine: Path
    prediction_archive: Path
    cycle_lock: Path
    history: Path
    market_audit_output: Path
    oddstorm_history_output: Path
    cycle_evidence: Path
    evaluation_evidence: Path
    crawl4ai_archive: Path
    openfootball_raw_archive_dir: Path
    sites_receipts_dir: Path


def resolve_runtime_paths(runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> RuntimePaths:
    """Return deterministic runtime paths below ``runtime_dir``.

    The returned values are path-only projections.  Callers remain responsible
    for atomic writes, permissions and the pre-flight storage check.
    """

    root = Path(runtime_dir)
    return RuntimePaths(
        root=root,
        live_path=root / "current.json",
        archive_dir=root / "archive",
        intelligence_ledger=root / "intelligence" / "observations.jsonl",
        intelligence_quarantine=root / "intelligence" / "observations.quarantine.jsonl",
        prediction_archive=root / "prospective_predictions.jsonl",
        cycle_lock=root / "prospective-cycle.lock",
        history=root / "prospective-cycle.jsonl",
        market_audit_output=root / "prospective_market_baselines.jsonl",
        oddstorm_history_output=root / "oddstorm_history.jsonl",
        cycle_evidence=root / "prospective-cycle-latest.json",
        evaluation_evidence=root / "prospective-evaluation-current.json",
        crawl4ai_archive=root / "crawl4ai",
        openfootball_raw_archive_dir=root / "openfootball-raw",
        sites_receipts_dir=root / "sites",
    )


def resolve_future_openfootball_raw_archive_dir(
    configured: Path | str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve only an explicitly configured durable future-model archive.

    The explicit argument takes precedence over the operator environment even
    when it is invalid.  This deliberately never derives a path from
    ``MATCHLINE_RUNTIME_DIR`` or :class:`RuntimePaths`: a relative/default
    runtime projection and a tmpfs mirror are not durable model evidence.

    Existence and archive integrity remain the consumer's fail-closed concern
    so a missing/corrupt archive can leave the read-only schedule available.
    """

    environment = os.environ if environ is None else environ
    raw_value: Path | str | None = configured
    if raw_value is None:
        raw_value = environment.get(OPENFOOTBALL_RAW_ARCHIVE_ENV)
    if raw_value is None:
        return None
    if isinstance(raw_value, str) and not raw_value.strip():
        return None

    candidate = Path(raw_value)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=False)
        volatile_root = Path("/dev/shm").resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        resolved.relative_to(volatile_root)
    except ValueError:
        return candidate
    return None


__all__ = [
    "DEFAULT_RUNTIME_DIR",
    "OPENFOOTBALL_RAW_ARCHIVE_ENV",
    "RuntimePaths",
    "resolve_future_openfootball_raw_archive_dir",
    "resolve_runtime_paths",
]
