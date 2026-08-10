"""Validate and attach as-of current-source snapshots to historical state."""

from __future__ import annotations

import json
import math
import string
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from league_platform.identity import team_id

EXPECTED_SOURCE_ROLES = {
    "fixtures_and_results": "ESPN",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "ESPN event summary / named bookmaker",
    "injuries_and_lineups": None,
}


def _validate_source(
    source: dict,
    as_of: datetime,
    *,
    hash_keys: tuple[str, ...],
    expected_name: str,
    allowed_hosts: set[str],
    require_all_hashes: bool = False,
) -> None:
    observed = datetime.fromisoformat(source["retrieved_at"])
    if observed.tzinfo is None or observed.astimezone(timezone.utc) > as_of.astimezone(
        timezone.utc
    ):
        raise ValueError(
            "current source retrieved_at must be timezone-aware and no later than as_of"
        )
    parsed_url = urlparse(source.get("url", ""))
    if parsed_url.scheme != "https" or parsed_url.hostname not in allowed_hosts:
        raise ValueError("current source URL or host is not allowlisted")
    if source.get("name") != expected_name:
        raise ValueError("current source name does not match its data role")
    hashes = [source.get(key) for key in hash_keys]
    valid_hashes = [
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
        for value in hashes
    ]
    if (require_all_hashes and not all(valid_hashes)) or (
        not require_all_hashes and not any(valid_hashes)
    ):
        raise ValueError("current source is missing a 64-character content hash")


def _validate_live_snapshot(live: dict, as_of: datetime, competition_ids: set[str]) -> None:
    valid_statuses = {"upcoming", "live", "finished", "postponed", "cancelled"}
    fixtures = live.get("espn", {}).get("fixtures", [])
    fixture_ids = {fixture.get("id") for fixture in fixtures}
    for fixture in fixtures:
        if fixture.get("competition_id") not in competition_ids:
            raise ValueError("current fixture has unknown competition")
        if fixture.get("status") not in valid_statuses:
            raise ValueError("current fixture has invalid status")
        kickoff = datetime.fromisoformat(fixture["kickoff_at"])
        if kickoff.tzinfo is None:
            raise ValueError("current fixture kickoff_at must be timezone-aware")
        source = fixture["source"]
        _validate_source(
            source,
            as_of,
            hash_keys=("raw_sha256",),
            expected_name="ESPN",
            allowed_hosts={"site.api.espn.com"},
        )
        if (
            not all(
                fixture.get(key)
                for key in ("id", "home_provider_team_id", "away_provider_team_id")
            )
            or source.get("native_fixture_id") is None
        ):
            raise ValueError("current fixture is missing provider-native IDs")
        score = fixture.get("score")
        if fixture["status"] == "finished" and (
            not isinstance(score, dict)
            or not all(
                isinstance(score.get(key), int) and score[key] >= 0 for key in ("home", "away")
            )
        ):
            raise ValueError("finished current fixture requires a non-negative integer score")
    for feature in live.get("understat", {}).get("team_features", []):
        if feature.get("competition_id") not in competition_ids:
            raise ValueError("current feature has unknown competition")
        _validate_source(
            feature["source"],
            as_of,
            hash_keys=("wire_sha256", "content_sha256"),
            expected_name="Understat",
            allowed_hosts={"understat.com"},
            require_all_hashes=True,
        )
        for key in ("xg_for", "xg_against"):
            if (
                key not in feature
                or not math.isfinite(float(feature[key]))
                or float(feature[key]) < 0
            ):
                raise ValueError("current xG feature must be present, finite and non-negative")
    for market in live.get("espn_markets", {}).get("markets", []):
        if market.get("fixture_id") not in fixture_ids or not market.get("provider"):
            raise ValueError("current market is missing a known fixture or provider")
        market_source = {**market["source"], "retrieved_at": market["retrieved_at"]}
        _validate_source(
            market_source,
            as_of,
            hash_keys=("raw_sha256",),
            expected_name="ESPN event summary",
            allowed_hosts={"site.api.espn.com"},
        )
        values = market.get("probability", {}).values()
        if set(market.get("probability", {})) != {"home", "draw", "away"} or not all(
            math.isfinite(float(value)) and 0 <= float(value) <= 1 for value in values
        ):
            raise ValueError("current market probabilities must be finite and bounded")
        if abs(sum(float(value) for value in values) - 1) > 1e-5:
            raise ValueError("current market probabilities must sum to one")


def attach_current_data(
    snapshot: dict,
    live_path: Path,
    *,
    now: datetime | None = None,
    freshness_limit: timedelta = timedelta(hours=6),
) -> dict:
    payload = deepcopy(snapshot)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if not live_path.exists():
        payload["current_data"] = {
            "status": "unavailable",
            "message": "尚未运行多源当前数据同步。",
            "as_of": None,
            "roles": {},
        }
        return payload

    live = json.loads(live_path.read_text(encoding="utf-8"))
    if live.get("schema_version") != "1.0.0" or live.get("roles") != EXPECTED_SOURCE_ROLES:
        raise ValueError("live snapshot schema_version or roles are invalid")
    provider_contracts = {
        "espn": ("ESPN", "fixtures"),
        "espn_markets": ("ESPN event summary", "markets"),
        "understat": ("Understat", "team_features"),
    }
    for key, (provider, records_key) in provider_contracts.items():
        source_payload = live.get(key)
        if (
            not isinstance(source_payload, dict)
            or source_payload.get("provider") != provider
            or not isinstance(source_payload.get(records_key), list)
            or not isinstance(source_payload.get("errors"), list)
        ):
            raise ValueError("live snapshot provider contract is invalid")
    as_of = datetime.fromisoformat(live["as_of"])
    if as_of.tzinfo is None:
        raise ValueError("live snapshot as_of must be timezone-aware")
    if as_of > checked_at + timedelta(minutes=5):
        raise ValueError("live snapshot as_of is in the future")
    competition_ids = {item["id"] for item in payload["competitions"]}
    _validate_live_snapshot(live, as_of, competition_ids)
    age = checked_at.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
    provider_errors = {
        "espn": live.get("espn", {}).get("errors", []),
        "espn_markets": live.get("espn_markets", {}).get("errors", []),
        "understat": live.get("understat", {}).get("errors", []),
    }
    fixtures_from_source = live.get("espn", {}).get("fixtures", [])
    fixture_competitions = {fixture.get("competition_id") for fixture in fixtures_from_source}
    platform_competitions = {item["id"] for item in payload["competitions"]}
    expected_competitions = set(live.get("expected_competitions", []))
    if expected_competitions != platform_competitions:
        raise ValueError("live snapshot expected_competitions are invalid")
    if not fixtures_from_source:
        status = "unavailable"
    elif any(provider_errors.values()) or fixture_competitions != expected_competitions:
        status = "degraded"
    else:
        status = "fresh" if age <= freshness_limit else "stale"

    features = live.get("understat", {}).get("team_features", [])
    markets = {
        item["fixture_id"]: item for item in live.get("espn_markets", {}).get("markets", [])
    }
    feature_team_ids = {team_id(item["competition_id"], item["team"]): item for item in features}
    fixtures = []
    for fixture in live.get("espn", {}).get("fixtures", []):
        competition_id = fixture["competition_id"]
        home_id = team_id(competition_id, fixture["home_team"])
        away_id = team_id(competition_id, fixture["away_team"])
        fixtures.append(
            {
                "id": fixture["id"],
                "competition_id": competition_id,
                "season": fixture["season"],
                "kickoff_at": fixture["kickoff_at"],
                "home_team": fixture["home_team"],
                "away_team": fixture["away_team"],
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": fixture["status"],
                "score": fixture["score"],
                "market_probability": None,
                "current_features": {
                    "home": feature_team_ids.get(home_id),
                    "away": feature_team_ids.get(away_id),
                    "market": markets.get(fixture["id"]),
                },
                "source": fixture["source"],
            }
        )
    ids = [fixture["id"] for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate current fixture IDs")

    for competition in payload["competitions"]:
        competition_id = competition["id"]
        competition_fixtures = [
            fixture for fixture in fixtures if fixture["competition_id"] == competition_id
        ]
        competition["current_source_status"] = status if competition_fixtures else "unavailable"
        competition["current_fixture_count"] = len(competition_fixtures)
        competition["current_xg_team_count"] = sum(
            item["competition_id"] == competition_id for item in features
        )

    payload["matches"].extend(fixtures)
    payload["matches"].sort(key=lambda item: item["kickoff_at"], reverse=True)
    payload["summary"]["current_fixture_count"] = len(fixtures)
    payload["summary"]["current_xg_team_count"] = len(features)
    payload["current_data"] = {
        "status": status,
        "message": (
            "当前赛程快照在新鲜度窗口内。"
            if status == "fresh"
            else "当前赛程快照已过期，未来预测被阻断。"
        ),
        "as_of": as_of.isoformat(),
        "age_seconds": max(0, int(age.total_seconds())),
        "roles": live.get("roles", {}),
        "provider_errors": provider_errors,
        "fixture_count": len(fixtures),
        "xg_team_count": len(features),
        "market_count": len(markets),
    }
    return payload
