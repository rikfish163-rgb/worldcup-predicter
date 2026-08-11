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
from league_platform.live_sources.news import ALLOWED_NEWS_HOSTS
from league_platform.live_sources.open_meteo import OPEN_METEO_HOST, OPEN_METEO_PATH
from league_platform.live_sources.sofascore import SOFASCORE_HOST

EXPECTED_SOURCE_ROLES = {
    "fixtures_and_results": "ESPN",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "ESPN event summary / named bookmaker",
    "injuries_and_lineups": "SofaScore provider-reported; not authoritative",
}
LEGACY_SOURCE_ROLES = {**EXPECTED_SOURCE_ROLES, "injuries_and_lineups": None}


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

    news = live.get("news")
    if news is not None:
        if (
            not isinstance(news, dict)
            or news.get("provider") != "Public RSS"
            or not isinstance(news.get("feeds"), list)
            or not isinstance(news.get("errors"), list)
            or not isinstance(news.get("item_count"), int)
        ):
            raise ValueError("current news provider contract is invalid")
        observed_items = 0
        for feed in news["feeds"]:
            if (
                not isinstance(feed, dict)
                or not isinstance(feed.get("name"), str)
                or not isinstance(feed.get("items"), list)
            ):
                raise ValueError("current news feed contract is invalid")
            parsed_url = urlparse(feed.get("url", ""))
            if parsed_url.hostname not in ALLOWED_NEWS_HOSTS:
                raise ValueError("current news URL or host is not allowlisted")
            _validate_source(
                feed,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name=feed["name"],
                allowed_hosts=ALLOWED_NEWS_HOSTS,
            )
            for item in feed["items"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("title"), str)
                    or not isinstance(item.get("link"), str)
                    or not item["link"].startswith("https://")
                ):
                    raise ValueError("current news item contract is invalid")
            observed_items += len(feed["items"])
        if news["item_count"] != observed_items:
            raise ValueError("current news item_count is inconsistent")

    weather = live.get("weather")
    if weather is not None:
        if (
            not isinstance(weather, dict)
            or weather.get("provider") != "Open-Meteo"
            or not isinstance(weather.get("weather"), list)
            or not isinstance(weather.get("errors"), list)
        ):
            raise ValueError("current weather provider contract is invalid")
        for observation in weather["weather"]:
            if not isinstance(observation, dict) or not observation.get("fixture_id"):
                raise ValueError("current weather observation contract is invalid")
            if observation["fixture_id"] not in fixture_ids:
                raise ValueError("current weather references an unknown fixture")
            source = observation.get("source")
            if not isinstance(source, dict):
                raise ValueError("current weather observation is missing provenance")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="Open-Meteo",
                allowed_hosts={OPEN_METEO_HOST},
            )
            if urlparse(source.get("url", "")).path != OPEN_METEO_PATH:
                raise ValueError("current weather URL path is not allowlisted")

    sofascore = live.get("sofascore")
    if sofascore is None:
        return
    if (
        not isinstance(sofascore, dict)
        or sofascore.get("provider") != "SofaScore"
        or not isinstance(sofascore.get("events"), list)
        or not isinstance(sofascore.get("errors"), list)
    ):
        raise ValueError("current SofaScore provider contract is invalid")
    for observation in sofascore["events"]:
        if not isinstance(observation, dict) or not observation.get("fixture_id"):
            raise ValueError("current SofaScore observation contract is invalid")
        if observation["fixture_id"] not in fixture_ids:
            raise ValueError("current SofaScore references an unknown fixture")
        for source_key in ("source", "lineups_source"):
            source = observation.get(source_key)
            if source is None:
                continue
            if not isinstance(source, dict):
                raise ValueError("current SofaScore provenance is invalid")
            _validate_source(
                source,
                as_of,
                hash_keys=("raw_sha256",),
                expected_name="SofaScore",
                allowed_hosts={SOFASCORE_HOST},
            )
            if not urlparse(source.get("url", "")).path.startswith("/api/v1/"):
                raise ValueError("current SofaScore URL path is not allowlisted")


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
    if live.get("schema_version") != "1.0.0" or live.get("roles") not in (
        EXPECTED_SOURCE_ROLES,
        LEGACY_SOURCE_ROLES,
    ):
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
    if "news" in live and not isinstance(live["news"], dict):
        raise ValueError("live snapshot news provider contract is invalid")
    if "weather" in live and not isinstance(live["weather"], dict):
        raise ValueError("live snapshot weather provider contract is invalid")
    if "sofascore" in live and not isinstance(live["sofascore"], dict):
        raise ValueError("live snapshot SofaScore provider contract is invalid")
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
        "news": live.get("news", {}).get("errors", []) if live.get("news") else [],
        "weather": live.get("weather", {}).get("errors", []) if live.get("weather") else [],
        "sofascore": live.get("sofascore", {}).get("errors", []) if live.get("sofascore") else [],
    }
    fixtures_from_source = live.get("espn", {}).get("fixtures", [])
    fixture_competitions = {fixture.get("competition_id") for fixture in fixtures_from_source}
    platform_competitions = {item["id"] for item in payload["competitions"]}
    expected_competitions = set(live.get("expected_competitions", []))
    if expected_competitions != platform_competitions:
        raise ValueError("live snapshot expected_competitions are invalid")
    if not fixtures_from_source:
        status = "unavailable"
    elif any(
        provider_errors[key] for key in ("espn", "espn_markets", "understat")
    ) or fixture_competitions != expected_competitions:
        status = "degraded"
    else:
        status = "fresh" if age <= freshness_limit else "stale"

    features = live.get("understat", {}).get("team_features", [])
    markets = {
        item["fixture_id"]: item for item in live.get("espn_markets", {}).get("markets", [])
    }
    weather = live.get("weather")
    weather_by_fixture = (
        {item["fixture_id"]: item for item in weather.get("weather", [])}
        if weather
        else {}
    )
    sofascore = live.get("sofascore")
    sofascore_by_fixture = (
        {item["fixture_id"]: item for item in sofascore.get("events", [])}
        if sofascore
        else {}
    )
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
                    "weather": weather_by_fixture.get(fixture["id"]),
                    "sofascore": sofascore_by_fixture.get(fixture["id"]),
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

    news = live.get("news")
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
        "news_status": (
            "fresh"
            if news and news.get("feeds") and not news.get("errors")
            else "degraded"
            if news and news.get("feeds")
            else "unavailable"
        ),
        "news_feed_count": len(news.get("feeds", [])) if news else 0,
        "news_item_count": news.get("item_count", 0) if news else 0,
        "weather_status": (
            "fresh"
            if weather and weather.get("weather") and not weather.get("errors")
            else "degraded"
            if weather and weather.get("weather")
            else "unavailable"
        ),
        "weather_count": len(weather.get("weather", [])) if weather else 0,
        "sofascore_status": (
            "fresh"
            if sofascore and sofascore.get("events") and not sofascore.get("errors")
            else "degraded"
            if sofascore and sofascore.get("events")
            else "unavailable"
        ),
        "sofascore_event_count": len(sofascore.get("events", [])) if sofascore else 0,
    }
    if news is not None:
        payload["news"] = news
    if weather is not None:
        payload["weather"] = weather
    if sofascore is not None:
        payload["sofascore"] = sofascore
    return payload
