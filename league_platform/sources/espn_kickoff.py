"""Validated ESPN historical kickoff-time enrichment.

The football-data cache contains many rows with a calendar date but no kickoff
time.  This module consumes a separately archived, hash-preserving mapping
from ESPN's public scoreboard responses.  The mapping is intentionally
limited to temporal ordering: it never supplies a result, odds, feature, or
lineup observation to the model.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.domain import Match


ESPN_HISTORY_HOST = "site.api.espn.com"
ESPN_HISTORY_PATH = re.compile(
    r"^/apis/site/v2/sports/soccer/(eng\.1|esp\.1|ger\.1|ita\.1|fra\.1)/scoreboard$"
)
MAX_ENRICHMENT_BYTES = 20 * 1024 * 1024
MIN_MATCH_CONFIDENCE = 0.90


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_source_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ESPN enrichment source_url is required")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ESPN_HISTORY_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not ESPN_HISTORY_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("ESPN enrichment source_url is not allowlisted")
    return value


def load_kickoff_enrichment(path: Path) -> dict[str, dict]:
    """Load and validate a compact, archived enrichment mapping.

    Keys are immutable provider fixture references such as
    ``E0_1516.csv:2``.  Invalid or ambiguous rows fail closed instead of being
    partially applied.
    """

    payload = path.read_bytes()
    if len(payload) > MAX_ENRICHMENT_BYTES:
        raise ValueError("ESPN kickoff enrichment exceeded 20 MiB")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("ESPN kickoff enrichment is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        raise ValueError("ESPN kickoff enrichment must contain a records list")
    records: dict[str, dict] = {}
    for row in document["records"]:
        if not isinstance(row, dict):
            raise ValueError("ESPN kickoff enrichment record must be an object")
        key = row.get("provider_fixture_id")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("ESPN kickoff enrichment record is missing provider_fixture_id")
        if key in records:
            raise ValueError(f"duplicate ESPN kickoff enrichment key: {key}")
        if row.get("role") != "temporal_order_only":
            raise ValueError(f"ESPN kickoff enrichment has an invalid role: {key}")
        source_hash = row.get("source_raw_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError(f"ESPN kickoff enrichment has an invalid source hash: {key}")
        row_sha = row.get("source_row_sha256")
        if not isinstance(row_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", row_sha):
            raise ValueError(f"ESPN kickoff enrichment has an invalid row hash: {key}")
        _validate_source_url(row.get("source_url"))
        _utc(row.get("kickoff_at"), field="kickoff_at")
        _utc(row.get("source_retrieved_at"), field="source_retrieved_at")
        confidence = row.get("match_confidence")
        if not isinstance(confidence, (int, float)) or confidence < MIN_MATCH_CONFIDENCE or confidence > 1:
            raise ValueError(f"ESPN kickoff enrichment confidence is below threshold: {key}")
        if not isinstance(row.get("competition_id"), str) or not isinstance(row.get("source_file"), str):
            raise ValueError(f"ESPN kickoff enrichment identity fields are invalid: {key}")
        records[key] = row
    return records


def apply_kickoff_enrichment(
    matches: Iterable[Match],
    records: dict[str, dict],
) -> tuple[list[Match], dict[str, int]]:
    """Apply only high-confidence date-only mappings and return audit counts."""

    # Callers may provide a streaming iterator from the source loader. Keep
    # one stable snapshot so the audit pass and replacement pass see the same
    # rows instead of consuming the iterator twice.
    matches = list(matches)
    enriched: list[Match] = []
    applied = 0
    skipped_exact = 0
    rejected_calendar_date = 0
    initial_date_only = sum(
        match.kickoff_time_quality == "date_only" for match in matches
    )
    missing = 0
    for match in matches:
        row = records.get(match.provider_fixture_id)
        if row is None:
            if match.kickoff_time_quality == "date_only":
                missing += 1
            enriched.append(match)
            continue
        if match.kickoff_time_quality != "date_only":
            skipped_exact += 1
            enriched.append(match)
            continue
        kickoff = _utc(row["kickoff_at"], field="kickoff_at")
        local_date = kickoff.astimezone(ZoneInfo(_match_timezone(match.competition_id))).date()
        if local_date != match.kickoff_at.date():
            rejected_calendar_date += 1
            enriched.append(match)
            continue
        enriched.append(
            replace(
                match,
                kickoff_at=kickoff,
                kickoff_time_quality="exact",
                kickoff_time_source="ESPN",
                kickoff_time_observed_at=row["source_retrieved_at"],
            )
        )
        applied += 1
    return enriched, {
        "applied": applied,
        "skipped_exact": skipped_exact,
        "rejected_calendar_date": rejected_calendar_date,
        "remaining_date_only": initial_date_only - applied,
    }


def _match_timezone(competition_id: str) -> str:
    timezones = {
        "premier-league": "Europe/London",
        "la-liga": "Europe/Madrid",
        "bundesliga": "Europe/Berlin",
        "serie-a": "Europe/Rome",
        "ligue-1": "Europe/Paris",
    }
    try:
        return timezones[competition_id]
    except KeyError as exc:
        raise ValueError(f"ESPN kickoff enrichment has unsupported competition: {competition_id}") from exc


__all__ = ["apply_kickoff_enrichment", "load_kickoff_enrichment"]
