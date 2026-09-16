"""Archive authorized OddStorm odds-history observations without changing the model.

The live OddStorm league page exposes numeric odds identifiers, while the
public ``/odds/oddhistory?id=...`` endpoint exposes provider timestamps for
those identifiers.  This module joins the two only for an append-only audit
sidecar.  It deliberately does *not* join the provider fixture to ESPN or
Sports Lottery and therefore never turns an observation into a model feature.

OddStorm's current terms prohibit automated scraping, mirroring,
redistribution and resale.  The endpoint is therefore fail-closed unless an
operator records an express written authorization reference.  Parser and
archive replay remain available for previously captured audit evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from league_platform.live_sources.oddstorm import (
    ODDSTORM_RIGHTS_STATUS,
    ODDSTORM_TERMS_URL,
    fetch_oddstorm_history,
)


DEFAULT_LIVE_PATH = Path("data/live/current.json")
DEFAULT_OUTPUT = Path("data/live/oddstorm_history.jsonl")
DEFAULT_MAX_REQUESTS = 24
ROTATION_SECONDS = 15 * 60


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _observation_key(row: Mapping[str, Any]) -> str | None:
    """Return a stable identity for one provider history observation.

    Capture time and snapshot time describe when we saw an observation, not
    the observation itself.  They must therefore not participate in
    append-only deduplication.  The key intentionally uses the provider's
    timestamp/price plus the fixture-side identity, so an actual price change
    remains a distinct observation while repeated polling does not duplicate
    an unchanged one.
    """

    history = row.get("history")
    if not isinstance(history, Mapping):
        return None
    line = _number(row.get("line"))
    if line is None:
        line = str(row.get("line") or "").strip()
    identity = {
        "record_type": row.get("record_type", "oddstorm_history"),
        "match_id": row.get("match_id"),
        "market": row.get("market"),
        "line": line,
        "side": row.get("side"),
        "odds_id": row.get("odds_id") or history.get("odds_id"),
        "decimal_odds": history.get("decimal_odds"),
        "provider_date_raw": history.get("provider_date_raw"),
        "effective_at": history.get("effective_at"),
    }
    required = (
        identity["match_id"],
        identity["market"],
        identity["line"],
        identity["side"],
        identity["odds_id"],
        identity["decimal_odds"],
        identity["provider_date_raw"],
    )
    if any(value in (None, "") for value in required):
        return None
    return _digest(identity)


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _candidates(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    section = snapshot.get("oddstorm")
    lines = section.get("lines") if isinstance(section, Mapping) else None
    if not isinstance(lines, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for line in lines:
        if not isinstance(line, Mapping):
            continue
        try:
            kickoff = _utc(line.get("kickoff_at"), field="line.kickoff_at")
        except ValueError:
            continue
        match_id = str(line.get("match_id") or "").strip()
        if not match_id:
            continue
        for market_name, market_key, side_keys, line_key in (
            (
                "asian_handicap",
                "handicap",
                (("home", "home_odds_id"), ("away", "away_odds_id")),
                "line",
            ),
            (
                "total",
                "total",
                (("over", "over_odds_id"), ("under", "under_odds_id")),
                "line",
            ),
        ):
            market = line.get(market_key)
            if not isinstance(market, Mapping):
                continue
            line_value = _number(market.get(line_key))
            if line_value is None:
                continue
            for side, id_key in side_keys:
                odds_id = str(market.get(id_key) or "").strip()
                if not odds_id.isdigit():
                    continue
                key = (odds_id, market_name, match_id)
                if key in seen:
                    continue
                seen.add(key)
                result.append(
                    {
                        "match_id": match_id,
                        "home_team": str(line.get("home_team") or ""),
                        "away_team": str(line.get("away_team") or ""),
                        "kickoff_at": kickoff.isoformat(),
                        "market": market_name,
                        "line": line_value,
                        "side": side,
                        "odds_id": odds_id,
                    }
                )
    result.sort(
        key=lambda item: (
            item["kickoff_at"],
            item["match_id"],
            item["market"],
            item["line"],
            item["side"],
            item["odds_id"],
        )
    )
    return result


def _read_existing(path: Path) -> tuple[set[str], list[dict[str, Any]]]:
    known: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    if not path.exists():
        return known, diagnostics
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return known, [{"reason": "read_error", "error": str(exc)}]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            diagnostics.append({"line": line_number, "reason": "invalid_json"})
            continue
        if not isinstance(row, Mapping):
            diagnostics.append({"line": line_number, "reason": "non_object"})
            continue
        observation_key = row.get("observation_key")
        if isinstance(observation_key, str) and len(observation_key) == 64:
            known.add(observation_key)
            continue
        # Rows written by the first implementation have only a volatile
        # content hash.  Derive the stable semantic key so the migration does
        # not require rewriting or deleting the append-only archive.
        derived = _observation_key(row)
        if derived is not None:
            known.add(derived)
    return known, diagnostics


def _selected(candidates: list[dict[str, Any]], *, captured_at: datetime, limit: int) -> tuple[list[dict[str, Any]], int]:
    if not candidates:
        return [], 0
    slot = int(captured_at.timestamp() // ROTATION_SECONDS)
    offset = (slot * limit) % len(candidates)
    count = min(limit, len(candidates))
    return [candidates[(offset + index) % len(candidates)] for index in range(count)], offset


def capture(
    live_path: Path = DEFAULT_LIVE_PATH,
    output: Path = DEFAULT_OUTPUT,
    *,
    now: datetime | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    authorization_reference: str | None = None,
) -> dict[str, Any]:
    """Fetch one rotating slice only with express written authorization."""

    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")
    snapshot = json.loads(live_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping):
        raise ValueError("live snapshot root must be an object")
    snapshot_as_of = _utc(snapshot.get("as_of"), field="snapshot.as_of")
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = _candidates(snapshot)
    authorization = (
        authorization_reference.strip()
        if isinstance(authorization_reference, str)
        and authorization_reference.strip()
        else None
    )
    if authorization is None:
        return {
            "status": "rights_blocked",
            "snapshot_as_of": snapshot_as_of.isoformat(),
            "captured_at": captured_at.isoformat(),
            "candidate_count": len(candidates),
            "selected_count": 0,
            "rotation_offset": 0,
            "fetched_count": 0,
            "history_rows": 0,
            "appended": 0,
            "read_diagnostics": [],
            "fetch_errors": [],
            "output": str(output),
            "access_allowed": False,
            "network_opened": False,
            "rights_status": ODDSTORM_RIGHTS_STATUS,
            "authorization_required": "express_written_permission",
            "terms_url": ODDSTORM_TERMS_URL,
        }
    selected, offset = _selected(candidates, captured_at=captured_at, limit=max_requests)
    known, read_diagnostics = _read_existing(output)
    pending: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    fetched = 0
    history_rows = 0
    for candidate in selected:
        fetched += 1
        result = fetch_oddstorm_history(
            candidate["odds_id"],
            fixture_kickoff_at=datetime.fromisoformat(candidate["kickoff_at"]),
            now=captured_at,
            authorization_reference=authorization,
        )
        if result.get("errors"):
            fetch_errors.extend(
                {
                    **error,
                    "odds_id": candidate["odds_id"],
                    "match_id": candidate["match_id"],
                }
                for error in result["errors"]
                if isinstance(error, Mapping)
            )
        for history in result.get("history", []):
            if not isinstance(history, Mapping):
                continue
            row = {
                "schema_version": "1.0.0",
                "record_type": "oddstorm_history",
                "snapshot_as_of": snapshot_as_of.isoformat(),
                "captured_at": captured_at.isoformat(),
                **candidate,
                "history": dict(history),
                "enters_model": False,
                "model_exclusion_reason": "independent_market_fixture_join_unproven",
            }
            observation_key = _observation_key(row)
            if observation_key is None:
                continue
            row["observation_key"] = observation_key
            row["content_sha256"] = _digest(row)
            if observation_key in known:
                continue
            known.add(observation_key)
            pending.append(row)
            history_rows += 1
    if pending:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            for row in pending:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "status": "available" if history_rows else "degraded" if fetch_errors else "empty",
        "snapshot_as_of": snapshot_as_of.isoformat(),
        "captured_at": captured_at.isoformat(),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "rotation_offset": offset,
        "fetched_count": fetched,
        "history_rows": history_rows,
        "appended": len(pending),
        "read_diagnostics": read_diagnostics,
        "fetch_errors": fetch_errors[:50],
        "output": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-path", type=Path, default=DEFAULT_LIVE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    args = parser.parse_args()
    print(json.dumps(capture(args.live_path, args.output, max_requests=args.max_requests), ensure_ascii=False))


__all__ = ["capture"]


if __name__ == "__main__":
    main()
