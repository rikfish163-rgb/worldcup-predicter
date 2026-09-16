"""Recent xG/form adapter for Understat's five supported European leagues."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


UNDERSTAT_SLUGS = {
    "premier-league": "EPL",
    "la-liga": "La_liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie_A",
    "ligue-1": "Ligue_1",
}
MAX_CONTENT_BYTES = 20 * 1024 * 1024
UNDERSTAT_HOST = "understat.com"


def decode_understat_payload(payload: bytes, *, max_bytes: int = MAX_CONTENT_BYTES) -> bytes:
    if not payload.startswith(b"\x1f\x8b"):
        if len(payload) > max_bytes:
            raise ValueError("Understat decompressed payload exceeded size limit")
        return payload
    chunks = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        while chunk := stream.read(min(1024 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Understat decompressed payload exceeded size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _validate_response_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != UNDERSTAT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Understat response redirected to a non-allowlisted URL")




def aggregate_understat_payload(
    payload: bytes, *, competition_id: str, retrieved_at: datetime, url: str
) -> list[dict]:
    data = json.loads(payload)
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    team_rows: dict[str, list[dict]] = defaultdict(list)
    for match in data.get("dates", []):
        if not match.get("isResult"):
            continue
        effective_at = datetime.fromisoformat(match["datetime"]).replace(tzinfo=timezone.utc)
        if effective_at >= retrieved_at.astimezone(timezone.utc):
            continue
        home = match["h"]
        away = match["a"]
        values = {
            "home": {
                "team": home,
                "xg_for": float(match["xG"]["h"]),
                "xg_against": float(match["xG"]["a"]),
                "goals_for": int(match["goals"]["h"]),
                "goals_against": int(match["goals"]["a"]),
            },
            "away": {
                "team": away,
                "xg_for": float(match["xG"]["a"]),
                "xg_against": float(match["xG"]["h"]),
                "goals_for": int(match["goals"]["a"]),
                "goals_against": int(match["goals"]["h"]),
            },
        }
        for value in values.values():
            team_rows[str(value["team"]["id"])].append({"effective_at": effective_at, **value})
    observations = []
    for provider_team_id, rows in team_rows.items():
        recent = sorted(rows, key=lambda item: item["effective_at"])[-5:]
        if not recent:
            continue
        observations.append(
            {
                "competition_id": competition_id,
                "team": recent[-1]["team"]["title"],
                "provider_team_id": provider_team_id,
                "sample_n": len(recent),
                "last_match_at": recent[-1]["effective_at"].isoformat(),
                "xg_for": round(sum(row["xg_for"] for row in recent) / len(recent), 4),
                "xg_against": round(sum(row["xg_against"] for row in recent) / len(recent), 4),
                "goals_for": round(sum(row["goals_for"] for row in recent) / len(recent), 4),
                "goals_against": round(
                    sum(row["goals_against"] for row in recent) / len(recent), 4
                ),
                "source": {
                    "name": "Understat",
                    "url": url,
                    "retrieved_at": retrieved_at.isoformat(),
                    "raw_sha256": raw_sha256,
                },
            }
        )
    return observations


def fetch_understat_features(*, now: datetime | None = None, opener=None) -> dict:
    """Return the v260 rights block before constructing an Understat opener."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    return rights_blocked_envelope(
        SourceId.UNDERSTAT_XG,
        provider="Understat",
        checked_at=reference_time.isoformat(),
        empty_fields=("observations",),
    )
