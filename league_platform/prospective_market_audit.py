"""Build an append-only market-baseline sidecar for frozen predictions.

The strict prediction archive is immutable once a prospective lock is active.
This sidecar therefore records the market probability used as a live feature
without rewriting the locked prediction payload.  Every row is joined through
the source response hash carried by ``live_feature_sources`` and is accepted
only when the source observation is at or before the freeze cutoff.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_PREDICTION_ARCHIVE = Path("data/live/prospective_predictions.jsonl")
DEFAULT_SNAPSHOT_ARCHIVE = Path("data/live/archive")
DEFAULT_OUTPUT = Path("data/live/prospective_market_baselines.jsonl")


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _probability(value: object) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or set(value) != {"home", "draw", "away"}:
        return None
    try:
        result = {key: float(value[key]) for key in ("home", "draw", "away")}
    except (KeyError, TypeError, ValueError):
        return None
    if any(number < 0 for number in result.values()):
        return None
    total = sum(result.values())
    if total <= 0 or abs(total - 1.0) > 1e-5:
        return None
    return {key: number / total for key, number in result.items()}


def _market_source(prediction: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for item in prediction.get("live_feature_sources", []):
        if not isinstance(item, Mapping) or item.get("field") != "market_1x2":
            continue
        if isinstance(item.get("raw_sha256"), str) and len(item["raw_sha256"]) == 64:
            return item
    return None


def _raw_market_index(snapshot_archive: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    raw_dir = snapshot_archive / "raw"
    for path in sorted(raw_dir.glob("*.json")):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot_as_of = _utc(snapshot.get("as_of"), field="snapshot.as_of")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        markets = snapshot.get("espn_markets", {}).get("markets", [])
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, Mapping):
                continue
            fixture_id = market.get("fixture_id")
            source = market.get("source")
            raw_hash = source.get("raw_sha256") if isinstance(source, Mapping) else None
            probability = _probability(market.get("probability"))
            retrieved = market.get("retrieved_at")
            if not isinstance(fixture_id, str) or not isinstance(raw_hash, str) or probability is None:
                continue
            try:
                retrieved_at = _utc(retrieved, field="market.retrieved_at")
            except ValueError:
                continue
            row = {
                "fixture_id": fixture_id,
                "probability": probability,
                "retrieved_at": retrieved_at.isoformat(),
                "snapshot_as_of": snapshot_as_of.isoformat(),
                "source": dict(source) if isinstance(source, Mapping) else {},
                "snapshot_path": str(path),
            }
            index.setdefault((fixture_id, raw_hash), []).append(row)
    for rows in index.values():
        rows.sort(key=lambda row: (row["retrieved_at"], row["snapshot_as_of"], row["snapshot_path"]))
    return index


def _build_rows(prediction_archive: Path, snapshot_archive: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index = _raw_market_index(snapshot_archive)
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    if not prediction_archive.exists():
        return rows, [{"reason": "prediction_archive_missing", "path": str(prediction_archive)}]
    for line_number, line in enumerate(prediction_archive.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            diagnostics.append({"line": line_number, "reason": "invalid_prediction_json"})
            continue
        prediction = record.get("prediction") if isinstance(record, Mapping) else None
        if not isinstance(prediction, Mapping) or record.get("conflict") is True:
            continue
        source = _market_source(prediction)
        if source is None:
            # A lineup-confirmation prediction is allowed to omit a market
            # feature when the public market source was unavailable at that
            # freeze.  Keep this distinct from a traceability failure on an
            # earlier stage (or from a row that claims a market probability
            # but has lost its source provenance).
            reason = "market_feature_source_missing"
            if (
                prediction.get("freeze_stage") == "lineup_confirmation"
                and prediction.get("market_probability") is None
            ):
                reason = "market_unavailable_at_lineup_stage"
            diagnostic: dict[str, Any] = {"line": line_number, "reason": reason}
            fixture_id = prediction.get("fixture_id")
            if isinstance(fixture_id, str) and fixture_id:
                diagnostic["fixture_id"] = fixture_id
            diagnostics.append(diagnostic)
            continue
        fixture_id = prediction.get("fixture_id")
        raw_hash = source["raw_sha256"]
        try:
            cutoff = _utc(prediction.get("freeze_cutoff_at"), field="prediction.freeze_cutoff_at")
        except ValueError as exc:
            diagnostics.append({"line": line_number, "reason": str(exc)})
            continue
        candidates = [
            item
            for item in index.get((fixture_id, raw_hash), [])
            if _utc(item["retrieved_at"], field="market.retrieved_at") <= cutoff
        ]
        if not candidates:
            diagnostics.append({"line": line_number, "reason": "no_causal_market_snapshot", "fixture_id": fixture_id})
            continue
        market = candidates[-1]
        frozen = _probability(prediction.get("scoreline_probability"))
        if frozen is None:
            diagnostics.append({"line": line_number, "reason": "frozen_probability_missing", "fixture_id": fixture_id})
            continue
        baseline = market["probability"]
        row = {
            "schema_version": "1.0.0",
            "freeze_key": record.get("freeze_key"),
            # The human-readable freeze model name stays stable across a
            # validated pre-result lock migration. Keep the immutable lock
            # digest in the sidecar identity so a changed market observation
            # under a new lock is not misclassified as a same-freeze conflict.
            "model_version_sha256": record.get("model_version_sha256"),
            "fixture_id": fixture_id,
            "competition_id": prediction.get("competition_id"),
            "freeze_stage": prediction.get("freeze_stage"),
            "freeze_cutoff_at": prediction.get("freeze_cutoff_at"),
            "model_version": prediction.get("model_version"),
            "frozen_probability": frozen,
            "market_probability": baseline,
            "probability_delta_model_minus_market": {
                key: frozen[key] - baseline[key] for key in ("home", "draw", "away")
            },
            "market_retrieved_at": market["retrieved_at"],
            "market_time_quality": "exact_source_retrieved_at",
            "market_time_bound_at": cutoff.isoformat(),
            "market_source": market["source"],
            "market_snapshot_path": market["snapshot_path"],
            "market_raw_sha256": raw_hash,
        }
        # The lock digest is an audit identity, not market content. Excluding
        # it keeps the sidecar idempotent across pre-result lock migrations
        # and lets existing rows (written before this field existed) match
        # without rewriting the append-only file.
        row["content_sha256"] = _sha256(
            {key: value for key, value in row.items() if key != "model_version_sha256"}
        )
        rows.append(row)
    return rows, diagnostics


def append_market_baselines(
    prediction_archive: Path = DEFAULT_PREDICTION_ARCHIVE,
    snapshot_archive: Path = DEFAULT_SNAPSHOT_ARCHIVE,
    output: Path = DEFAULT_OUTPUT,
) -> dict[str, int]:
    """Append causal market baselines idempotently and preserve conflicts."""

    rows, diagnostics = _build_rows(prediction_archive, snapshot_archive)
    existing: list[dict[str, Any]] = []
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.append(json.loads(line))
    known = {str(row.get("content_sha256")) for row in existing}
    by_freeze: dict[tuple[str, str], set[str]] = {}
    for row in existing:
        identity = (
            str(row.get("model_version_sha256") or "legacy"),
            str(row.get("freeze_key")),
        )
        by_freeze.setdefault(identity, set()).add(str(row.get("content_sha256")))
    pending: list[dict[str, Any]] = []
    conflicts = 0
    skipped = 0
    for row in rows:
        digest = row["content_sha256"]
        if digest in known:
            skipped += 1
            continue
        freeze_key = str(row.get("freeze_key"))
        identity = (str(row.get("model_version_sha256") or "legacy"), freeze_key)
        conflict_with = sorted(value for value in by_freeze.get(identity, set()) if value != digest)
        record = {
            **row,
            "record_key": _sha256({"freeze_key": freeze_key, "content_sha256": digest}),
            "conflict": bool(conflict_with),
            "conflict_with": conflict_with,
        }
        pending.append(record)
        known.add(digest)
        by_freeze.setdefault(identity, set()).add(digest)
        conflicts += int(bool(conflict_with))
    if pending:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            for row in pending:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "appended": len(pending),
        "skipped_duplicate": skipped,
        "conflicts": conflicts,
        "diagnostics": len(diagnostics),
        "diagnostic_reasons": dict(
            Counter(str(item.get("reason") or "unknown") for item in diagnostics)
        ),
        # Keep enough context to audit this cycle without making the cycle
        # evidence grow with the full immutable archive.
        "diagnostic_items": diagnostics[:50],
        "matched_rows": len(rows),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction-archive", type=Path, default=DEFAULT_PREDICTION_ARCHIVE)
    parser.add_argument("--snapshot-archive", type=Path, default=DEFAULT_SNAPSHOT_ARCHIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(append_market_baselines(args.prediction_archive, args.snapshot_archive, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["append_market_baselines"]
