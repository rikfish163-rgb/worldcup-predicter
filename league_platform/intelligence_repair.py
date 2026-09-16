"""Quarantine malformed intelligence-ledger lines without rewriting history.

The append-only observations ledger is the source of truth.  This utility
never removes or edits a ledger byte.  It records a line hash, the diagnostic,
and any complete observation objects that can be recovered from a concatenated
line in a separate JSONL quarantine manifest.  Recovered objects are audit
metadata only and are explicitly excluded from model input and publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from league_platform.intelligence import recover_observation_fragments

try:  # pragma: no cover - Linux is the deployed runtime.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


DEFAULT_LEDGER = Path("data/live/intelligence/observations.jsonl")
DEFAULT_QUARANTINE = Path("data/live/intelligence/observations.quarantine.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _line_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _lock_path(ledger_path: Path):
    class _Lock:
        def __init__(self) -> None:
            self.handle = None

        def __enter__(self):
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = Path(f"{ledger_path}.lock").open("a+")
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc, traceback):
            if self.handle is None:
                return False
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
            return False

    return _Lock()


def _existing_quarantine_keys(path: Path) -> set[tuple[int, str]]:
    keys: set[tuple[int, str]] = set()
    if not path.is_file():
        return keys
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            number = value.get("line")
            digest = value.get("line_sha256")
            if isinstance(number, int) and isinstance(digest, str):
                keys.add((number, digest))
    return keys


def _recovered_summary(fragments: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for fragment in fragments:
        value = fragment.get("observation")
        if not isinstance(value, dict):
            continue
        result.append(
            {
                "offset": fragment.get("offset"),
                "end": fragment.get("end"),
                "record_digest": fragment.get("record_digest"),
                "entity_type": value.get("entity_type"),
                "entity_id": value.get("entity_id"),
                "kind": value.get("kind"),
                "source_name": value.get("source_name"),
                "enters_model": False,
                "model_exclusion_reason": "recovered_from_quarantined_ledger_line",
            }
        )
    return result


def quarantine_ledger(
    ledger_path: Path = DEFAULT_LEDGER,
    quarantine_path: Path = DEFAULT_QUARANTINE,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Audit malformed lines and optionally append quarantine receipts."""

    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "ledger_path": str(ledger_path),
        "quarantine_path": str(quarantine_path),
        "scanned_lines": 0,
        "malformed_lines": 0,
        "recoverable_records": 0,
        "appended": 0,
        "skipped_existing": 0,
        "write": write,
        "entries": [],
    }
    if not ledger_path.is_file():
        result["status"] = "missing"
        return result

    with _lock_path(ledger_path):
        existing = _existing_quarantine_keys(quarantine_path)
        pending: list[dict[str, Any]] = []
        with ledger_path.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                result["scanned_lines"] += 1
                text = raw.decode("utf-8", errors="replace")
                try:
                    value = json.loads(text)
                    if not isinstance(value, dict):
                        raise ValueError("JSON root must be an object")
                    continue
                except (json.JSONDecodeError, ValueError) as exc:
                    result["malformed_lines"] += 1
                    reason = str(exc)
                digest = _line_sha256(raw)
                key = (line_number, digest)
                if key in existing:
                    result["skipped_existing"] += 1
                    continue
                fragments = recover_observation_fragments(text)
                recovered = _recovered_summary(fragments)
                result["recoverable_records"] += len(recovered)
                entry: dict[str, Any] = {
                    "schema_version": "1.0.0",
                    "created_at": _now(),
                    "ledger_path": str(ledger_path),
                    "line": line_number,
                    "bytes": len(raw),
                    "line_sha256": digest,
                    "status": "quarantined",
                    "reason": reason,
                    "recovered_records": recovered,
                    "model_use": "excluded",
                    "original_history_preserved": True,
                }
                pending.append(entry)
                if len(result["entries"]) < 20:
                    result["entries"].append(entry)
        if write and pending:
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            with quarantine_path.open("a", encoding="utf-8") as output:
                for entry in pending:
                    output.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            result["appended"] = len(pending)
    result["status"] = "quarantined" if result["malformed_lines"] else "clean"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--quarantine", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument(
        "--write",
        action="store_true",
        help="append quarantine receipts; without this flag, perform a dry-run",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            quarantine_ledger(args.ledger, args.quarantine, write=args.write),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = ["quarantine_ledger"]
