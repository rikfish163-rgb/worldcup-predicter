"""Persist official lineup polling results without inventing publication times.

The fetcher returns a complete lineup together with the exact response hash
and the local observation time. This adapter turns those rows into the same
append-only intelligence ledger used by other sources. A lineup can be a model
feature only when the upstream parser has already proved it was observed
before kickoff; a post-kickoff observation is retained for audit but is
explicitly non-model.
"""

from __future__ import annotations

from pathlib import Path

from league_platform.intelligence import ObservationLedger, observation_from_payload


def archive_premier_league_lineups(result: dict, ledger_path: Path) -> dict[str, int]:
    """Append official lineup observations and preserve source-level errors."""

    if not isinstance(result, dict) or not isinstance(result.get("lineups"), list):
        raise ValueError("official lineup result must contain a lineups list")
    observations = []
    skipped = 0
    for row in result["lineups"]:
        if not isinstance(row, dict):
            skipped += 1
            continue
        source = row.get("source")
        lineups = row.get("lineups")
        if not isinstance(source, dict) or not isinstance(lineups, dict):
            skipped += 1
            continue
        required = ("url", "retrieved_at", "raw_sha256")
        if any(not isinstance(source.get(key), str) or not source[key] for key in required):
            skipped += 1
            continue
        entity_id = row.get("fixture_id") or f"premierleague:{row.get('match_id')}"
        if not isinstance(entity_id, str) or entity_id.endswith(":None"):
            skipped += 1
            continue
        confirmed = bool(lineups.get("confirmed"))
        observations.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=entity_id,
                kind="official_lineup",
                payload={
                    "match_id": row.get("match_id"),
                    "fixture_id": row.get("fixture_id"),
                    "lineups": lineups,
                    "time_basis": source.get("time_basis"),
                },
                source_name=str(source.get("name") or "Premier League official"),
                source_url=source["url"],
                source_tier="official",
                observed_at=source["retrieved_at"],
                published_at=source.get("effective_at"),
                confidence=0.99 if confirmed else 0.75,
                enters_model=bool(lineups.get("model_eligible")),
                raw_hash=source["raw_sha256"],
            )
        )
    counts = ObservationLedger(ledger_path).append(observations)
    return {
        **counts,
        "skipped": skipped,
        "source_errors": len(result.get("errors") or []),
    }


__all__ = ["archive_premier_league_lineups"]
