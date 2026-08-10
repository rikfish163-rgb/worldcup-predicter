"""Current fixtures/results adapter for ESPN's public scoreboard feed."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable


ESPN_CODES = {
    "premier-league": "eng.1",
    "la-liga": "esp.1",
    "bundesliga": "ger.1",
    "serie-a": "ita.1",
    "ligue-1": "fra.1",
    "csl": "chn.1",
}
STATUS_MAP = {
    "STATUS_SCHEDULED": "upcoming",
    "STATUS_IN_PROGRESS": "live",
    "STATUS_HALFTIME": "live",
    "STATUS_FULL_TIME": "finished",
    "STATUS_FINAL_AET": "finished",
    "STATUS_FINAL_PEN": "finished",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "cancelled",
}


def parse_espn_payload(
    payload: bytes, *, competition_id: str, retrieved_at: datetime, url: str
) -> list[dict]:
    data = json.loads(payload)
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    fixtures = []
    for event in data.get("events", []):
        competition = event["competitions"][0]
        competitors = {item["homeAway"]: item for item in competition["competitors"]}
        if set(competitors) != {"home", "away"}:
            continue
        home = competitors["home"]
        away = competitors["away"]
        status = STATUS_MAP.get(event["status"]["type"]["name"], "upcoming")
        score = None
        if status == "finished":
            score = {
                "home": int(float(home.get("score", 0))),
                "away": int(float(away.get("score", 0))),
            }
        fixtures.append(
            {
                "id": f"espn:{event['id']}",
                "competition_id": competition_id,
                "season": str(event.get("season", {}).get("year") or event["date"][:4]),
                "kickoff_at": event["date"].replace("Z", "+00:00"),
                "home_team": home["team"]["displayName"],
                "away_team": away["team"]["displayName"],
                "home_provider_team_id": str(home["team"]["id"]),
                "away_provider_team_id": str(away["team"]["id"]),
                "status": status,
                "score": score,
                "source": {
                    "name": "ESPN",
                    "url": url,
                    "native_fixture_id": str(event["id"]),
                    "retrieved_at": retrieved_at.isoformat(),
                    "raw_sha256": raw_sha256,
                },
            }
        )
    return fixtures


def fetch_espn_fixtures(
    *,
    now: datetime | None = None,
    horizon_days: int = 45,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> dict:
    retrieved_at = now or datetime.now(timezone.utc)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    end = retrieved_at + timedelta(days=horizon_days)
    date_range = f"{retrieved_at:%Y%m%d}-{end:%Y%m%d}"
    fixtures = []
    errors = []
    for competition_id, code in ESPN_CODES.items():
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/soccer/"
            f"{code}/scoreboard?dates={date_range}&limit=500"
        )
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS ESPN host
            url,
            headers={"Accept": "application/json", "User-Agent": "Matchline/1.0"},
        )
        try:
            with opener(request, timeout=30) as response:  # noqa: S310
                payload = response.read(10 * 1024 * 1024 + 1)
            if len(payload) > 10 * 1024 * 1024:
                raise RuntimeError("ESPN response exceeded 10 MiB")
            fixtures.extend(
                parse_espn_payload(
                    payload,
                    competition_id=competition_id,
                    retrieved_at=retrieved_at,
                    url=url,
                )
            )
        except Exception as exc:  # source-level isolation is part of the contract
            errors.append({"competition_id": competition_id, "error": str(exc)})
    fixtures.sort(key=lambda item: item["kickoff_at"])
    return {
        "provider": "ESPN",
        "retrieved_at": retrieved_at.isoformat(),
        "horizon_days": horizon_days,
        "fixtures": fixtures,
        "errors": errors,
    }
