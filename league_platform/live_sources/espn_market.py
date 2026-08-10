"""Per-fixture current market adapter from ESPN event summaries."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from league_platform.live_sources.espn import ESPN_CODES


def _american_probability(odds: float) -> float:
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def parse_espn_market(
    payload: bytes, *, fixture_id: str, retrieved_at: datetime, url: str
) -> dict | None:
    data = json.loads(payload)
    candidates = data.get("pickcenter") or []
    if not candidates:
        return None
    market = candidates[0]
    try:
        raw = {
            "home": float(market["homeTeamOdds"]["moneyLine"]),
            "draw": float(market["drawOdds"]["moneyLine"]),
            "away": float(market["awayTeamOdds"]["moneyLine"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value != 0 for value in raw.values()):
        return None
    implied = {key: _american_probability(value) for key, value in raw.items()}
    total = sum(implied.values())
    return {
        "fixture_id": fixture_id,
        "provider": market.get("provider", {}).get("name", "unknown"),
        "american_odds": raw,
        "probability": {key: round(value / total, 6) for key, value in implied.items()},
        "retrieved_at": retrieved_at.isoformat(),
        "source": {
            "name": "ESPN event summary",
            "url": url,
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def fetch_espn_markets(
    fixtures: list[dict],
    *,
    now: datetime | None = None,
    horizon_days: int = 45,
    max_fixtures: int = 120,
) -> dict:
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    cutoff = reference_time.astimezone(timezone.utc) + timedelta(days=horizon_days)
    candidates = sorted(
        (
            fixture
            for fixture in fixtures
            if fixture["status"] == "upcoming"
            and datetime.fromisoformat(fixture["kickoff_at"]).astimezone(timezone.utc) <= cutoff
        ),
        key=lambda fixture: fixture["kickoff_at"],
    )[:max_fixtures]

    def fetch_one(fixture: dict) -> tuple[dict, bytes, str]:
        code = ESPN_CODES[fixture["competition_id"]]
        native_id = fixture["source"]["native_fixture_id"]
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/soccer/"
            f"{code}/summary?event={native_id}"
        )
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS ESPN host
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = response.read(10 * 1024 * 1024 + 1)
        if len(payload) > 10 * 1024 * 1024:
            raise RuntimeError("ESPN summary exceeded 10 MiB")
        return fixture, payload, url

    observations = []
    errors = []
    observation_times = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_one, fixture): fixture for fixture in candidates}
        for future in as_completed(futures):
            fixture = futures[future]
            try:
                _, payload, url = future.result()
                observed_at = reference_time if now is not None else datetime.now(timezone.utc)
                observation_times.append(observed_at)
                observation = parse_espn_market(
                    payload,
                    fixture_id=fixture["id"],
                    retrieved_at=observed_at,
                    url=url,
                )
                if observation:
                    observations.append(observation)
            except Exception as exc:
                errors.append({"fixture_id": fixture["id"], "error": str(exc)})
    observations.sort(key=lambda item: item["fixture_id"])
    return {
        "provider": "ESPN event summary",
        "retrieved_at": max(observation_times, default=reference_time).isoformat(),
        "requested_fixtures": len(candidates),
        "markets": observations,
        "errors": errors,
    }
