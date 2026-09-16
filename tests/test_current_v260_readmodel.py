from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.current import (
    EXPECTED_SOURCE_ROLES,
    _compatible_source_roles,
    _validate_live_snapshot,
    attach_current_data,
)
from league_platform.live_sources.openligadb import parse_openligadb_payload
from league_platform.sync_live import _sync_unlocked


AS_OF = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
SHA256 = "a" * 64
V260_ROLES = {
    "fixtures_and_results": (
        "OpenFootball CC0 current-cycle rows rebuilt from durable raw evidence only; "
        "stale replay remains disabled"
    ),
    "secondary_current_results": (
        "OpenLigaDB ODbL isolated serving lane; excluded from fixture_feed "
        "and every model/training/redistribution lane"
    ),
    "historical_training": ("OpenFootball durable raw archive verified consumer required"),
    "blocked_current_sources": ("All other v260 sources are canonical pre-network rights blocks"),
    "raw_archive_replay": "current_cycle_reparse_only_no_stale_replay",
}


def _base_snapshot() -> dict:
    competition_ids = (
        "bundesliga",
        "championship",
        "csl",
        "la-liga",
        "ligue-1",
        "premier-league",
        "serie-a",
    )
    return {
        "competitions": [{"id": competition_id} for competition_id in competition_ids],
        "matches": [],
        "summary": {
            "current_fixture_count": 0,
            "current_xg_team_count": 0,
        },
    }


def _openfootball_fixture() -> dict:
    source = {
        "name": "OpenFootball",
        "source_id": "openfootball:football.json:2026-27:en.1",
        "url": (
            "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json"
        ),
        "retrieved_at": AS_OF.isoformat(),
        "raw_sha256": SHA256,
        "hash": f"sha256:{SHA256}",
        "license": "CC0-1.0",
        # These are deliberately self-reported and must not authorize a model.
        "model_eligible": True,
        "producer_receipt": {"status": "completed", "raw_sha256": SHA256},
    }
    return {
        "id": "openfootball:premier-league:fixture-one",
        "competition_id": "premier-league",
        "season": "2026-27",
        "round": "1",
        "kickoff_date": "2026-08-26",
        "kickoff_at": "2026-08-26T18:00:00+00:00",
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "OpenFootball",
        "home_team": "Arsenal FC",
        "away_team": "Manchester City FC",
        "status": "upcoming",
        "score": None,
        "halftime_score": None,
        "result_scope": None,
        "provider_fixture_ids": {"OpenFootball": "openfootball:premier-league:fixture-one"},
        "source": source,
        "lineage": {
            **source,
            "source_row_id": ("openfootball:football.json:2026-27:en.1#$.matches[0]"),
            "record_path": "$.matches[0]",
            "raw_archive_receipt": {"status": "completed", "raw_sha256": SHA256},
        },
    }


def _openligadb_match() -> dict:
    raw = json.dumps(
        [
            {
                "matchID": 123,
                "matchDateTimeUTC": "2026-08-27T18:30:00Z",
                "leagueSeason": 2026,
                "matchIsFinished": False,
                "team1": {"teamId": 1, "teamName": "Home"},
                "team2": {"teamId": 2, "teamName": "Away"},
                "matchResults": [],
            }
        ]
    ).encode()
    return parse_openligadb_payload(
        raw,
        retrieved_at=AS_OF,
        url="https://api.openligadb.de/getmatchdata/bl1/2026",
    )[0]


def _v260_live() -> dict:
    fixture = _openfootball_fixture()
    return {
        "schema_version": "1.0.0",
        "as_of": AS_OF.isoformat(),
        "expected_competitions": [
            "bundesliga",
            "championship",
            "csl",
            "la-liga",
            "ligue-1",
            "premier-league",
            "serie-a",
        ],
        "roles": V260_ROLES,
        "fixture_feed": {
            "provider": "OpenFootball",
            "retrieved_at": AS_OF.isoformat(),
            "status": "ok",
            "errors": [],
            "fixtures": [fixture],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [fixture],
            "upcoming_7_days": [fixture],
            "source_contract": {
                "fact_source": "OpenFootball",
                "admission_status": "verified_current_raw",
                "raw_archive_producer_receipt": {
                    "status": "completed",
                    "raw_sha256": SHA256,
                },
            },
            "raw_archive_admission": {
                "status": "verified_current_raw",
                "rows_sha256": SHA256,
            },
        },
        "openfootball_current": {
            "provider": "OpenFootball",
            "fixtures": [fixture],
            "raw_archive_admission": {
                "status": "verified_current_raw",
                "rows_sha256": SHA256,
            },
        },
        "openligadb": {
            "provider": "OpenLigaDB",
            "retrieved_at": AS_OF.isoformat(),
            "status": "ok",
            "matches": [_openligadb_match()],
            "errors": [],
            "display_lane": "isolated_current_only",
            "fixture_feed_eligible": False,
            "material_feature_eligible": False,
            "model_eligible": False,
            "training_eligible": False,
            "redistribution_allowed": False,
        },
        # Every value below is intentionally fabricated. The v260 read-model
        # must quarantine by central source identity before projecting facts.
        "understat": {"provider": "Understat", "team_features": [{"xg": 99}]},
        "espn_markets": {
            "provider": "ESPN event summary",
            "markets": [{"fixture_id": fixture["id"], "probability": {"home": 1}}],
            "team_status": [{"fixture_id": fixture["id"], "status": "confirmed_lineup"}],
        },
        "espn_injuries": {"provider": "ESPN injury report", "reports": [{"fake": 1}]},
        "news": {"provider": "Public RSS", "feeds": [{"title": "fabricated"}]},
        "weather": {"provider": "Open-Meteo", "weather": [{"fixture_id": fixture["id"]}]},
        "geocoding": {
            "provider": "Open-Meteo Geocoding",
            "geocodes": [{"fixture_id": fixture["id"]}],
        },
        "sofascore": {"provider": "SofaScore", "events": [{"fixture_id": fixture["id"]}]},
        "fotmob": {"provider": "FotMob", "lineups": [{"fixture_id": fixture["id"]}]},
        "premier_league_official": {
            "provider": "Premier League official",
            "lineups": [{"fake": 1}],
        },
        "sports_lottery": {"provider": "Sports Lottery", "matches": [{"match_id": "fake"}]},
        "oddstorm": {"provider": "OddStorm", "lines": [{"match_id": "fake"}]},
        "crawl4ai": {"provider": "Crawl4AI", "pages": [{"url": "https://example.invalid"}]},
    }


def test_latest_sync_v260_roles_are_an_exact_compatible_contract() -> None:
    assert _compatible_source_roles(V260_ROLES)


def test_v260_projection_quarantines_all_blocked_ancillary_and_raw_self_claims(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(_v260_live()), encoding="utf-8")

    result = attach_current_data(_base_snapshot(), live_path, now=AS_OF)

    current = result["current_data"]
    assert current["role_contract"] == {
        "generation": "v260",
        "model_admission": False,
        "purpose": "research_display_only",
    }
    assert current["model_admission"]["eligible"] is False
    assert current["model_admission"]["reason"] == "consumer_raw_replay_required"
    row = next(
        item
        for item in result["matches"]
        if item.get("id") == "openfootball:premier-league:fixture-one"
    )
    assert row["current_features"] == {}
    assert row["market_probability"] is None
    assert row["model_admission"] == {
        "eligible": False,
        "enters_model": False,
        "reason": "consumer_raw_replay_required",
        "status": "unverified",
    }
    assert row["source_verification"] == {
        "status": "unverified",
        "verified_by_consumer": False,
        "reason": "consumer_raw_replay_required",
    }
    assert row["source"]["model_eligible"] is False
    assert "producer_receipt" not in row["source"]
    assert "raw_archive_receipt" not in row["lineage"]

    forbidden_model_keys = {
        "feature_times",
        "providers",
        "coverage",
        "factor_trace",
        "factorTrace",
    }
    assert forbidden_model_keys.isdisjoint(row)
    assert not any(item.get("id", "").startswith("sporttery:") for item in result["matches"])
    for blocked_key in (
        "understat",
        "espn_markets",
        "espn_injuries",
        "news",
        "weather",
        "geocoding",
        "sofascore",
        "fotmob",
        "premier_league_official",
        "sports_lottery",
        "oddstorm",
        "crawl4ai",
    ):
        assert blocked_key not in result


def test_openligadb_is_only_a_top_level_local_display_lane(tmp_path: Path) -> None:
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(_v260_live()), encoding="utf-8")

    result = attach_current_data(_base_snapshot(), live_path, now=AS_OF)

    assert result["openligadb"]["display_lane"] == "isolated_current_only"
    assert result["openligadb"]["model_eligible"] is False
    assert result["openligadb"]["matches"][0]["id"] == "openligadb:123"
    assert not any(row.get("id") == "openligadb:123" for row in result["matches"])
    assert (
        "openligadb"
        not in json.dumps(
            [row.get("current_features") for row in result["matches"]],
            ensure_ascii=False,
        ).lower()
    )


def test_openfootball_extra_league_is_display_only_and_does_not_break_current_readmodel(
    tmp_path: Path,
) -> None:
    live = _v260_live()
    extra = deepcopy(live["fixture_feed"]["fixtures"][0])
    extra["id"] = "openfootball:eredivisie:fixture-extra"
    extra["competition_id"] = "eredivisie"
    extra["source"] = {
        **extra["source"],
        "source_id": "openfootball:europe:netherlands:2026-27-nl1",
        "url": "https://raw.githubusercontent.com/openfootball/europe/master/netherlands/2026-27_nl1.txt",
    }
    extra["lineage"] = {
        **extra["lineage"],
        "source_id": "openfootball:europe:netherlands:2026-27-nl1",
        "url": extra["source"]["url"],
    }
    live["fixture_feed"]["fixtures"].append(extra)
    live_path = tmp_path / "current-extra-league.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    result = attach_current_data(_base_snapshot(), live_path, now=AS_OF)

    assert result["current_data"]["canonical_fixture_count"] == 1
    assert result["current_data"]["display_only_fixture_count"] == 1
    assert not any(row.get("id") == extra["id"] for row in result["matches"])
    display_rows = result["current_data"]["display_only_fixtures"]
    assert display_rows == [
        {
            "competition_id": "eredivisie",
            "fixture_id": extra["id"],
            "kickoff_at": extra["kickoff_at"],
            "kickoff_date": extra["kickoff_date"],
            "home_team": extra["home_team"],
            "away_team": extra["away_team"],
            "status": "display_only",
            "reason": "competition_not_in_model_catalog",
        }
    ]


def test_missing_sofascore_does_not_skip_later_lottery_validation() -> None:
    live = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [],
        },
        "sports_lottery": "malformed",
    }

    with pytest.raises(ValueError, match="Sports Lottery provider contract"):
        _validate_live_snapshot(live, AS_OF, {"premier-league"})


def _met_weather_live_section(*, provider: str = "MET Norway Locationforecast") -> dict:
    fixture = _openfootball_fixture()
    return {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [fixture],
            "errors": [],
        },
        "met_norway_weather": {
            "provider": provider,
            "source_id": "met_norway_weather",
            "status": "ok",
            "retrieved_at": AS_OF.isoformat(),
            "network_opened": True,
            "weather": [
                {
                    "fixture_id": fixture["id"],
                    "forecast_at": "2026-08-26T18:00:00+00:00",
                    "latitude": 51.5,
                    "longitude": -0.28,
                    "temperature_c": 18.0,
                    "humidity_percent": 70.0,
                    "precipitation_mm": 0.2,
                    "source": {
                        "name": "MET Norway Locationforecast",
                        "source_id": "met_norway_weather",
                        "url": (
                            "https://api.met.no/weatherapi/locationforecast/2.0/compact?"
                            "lat=51.5&lon=-0.28"
                        ),
                        "retrieved_at": AS_OF.isoformat(),
                        "raw_sha256": "b" * 64,
                        "license": "CC-BY-4.0",
                        "license_url": "https://creativecommons.org/licenses/by/4.0/",
                        "terms_url": "https://api.met.no/doc/TermsOfService",
                        "attribution_required": True,
                    },
                }
            ],
            "errors": [],
            "license": "CC-BY-4.0",
            "attribution_required": True,
        },
    }


def test_met_norway_weather_section_has_an_allowlisted_provider_contract() -> None:
    _validate_live_snapshot(
        _met_weather_live_section(),
        AS_OF,
        {"premier-league"},
    )


def test_met_norway_weather_section_rejects_a_relabelled_provider() -> None:
    with pytest.raises(ValueError, match="MET Norway weather provider contract"):
        _validate_live_snapshot(
            _met_weather_live_section(provider="Open-Meteo"),
            AS_OF,
            {"premier-league"},
        )


def test_met_norway_weather_coordinate_confidence_is_explicit_and_non_model_for_medium_rows() -> (
    None
):
    live = _met_weather_live_section()
    observation = live["met_norway_weather"]["weather"][0]
    observation["coordinate_confidence"] = "medium"
    observation["coordinate_model_eligible"] = False

    _validate_live_snapshot(live, AS_OF, {"premier-league"})

    observation["coordinate_model_eligible"] = True
    with pytest.raises(ValueError, match="MET Norway coordinate confidence contract"):
        _validate_live_snapshot(live, AS_OF, {"premier-league"})


def _wikidata_venue_live_section() -> dict:
    fixture = _openfootball_fixture()
    source = {
        "name": "Wikidata structured data",
        "source_id": "wikidata_entities",
        "url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q163995",
        "retrieved_at": AS_OF.isoformat(),
        "raw_sha256": "b" * 64,
        "license": "CC0-1.0",
        "license_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "attribution_required": False,
    }
    venue = {
        "name": "Emirates Stadium",
        "wikidata_id": "Q163995",
        "latitude": 51.555,
        "longitude": -0.1083333333,
        "claim_rank": "preferred",
        "confidence": "high",
        "model_eligible": True,
        "source": source,
    }
    fixture["venue"] = deepcopy(venue)
    fixture["field_sources"] = {"venue": deepcopy(source)}
    return {
        "fixture_feed": {"provider": "OpenFootball", "fixtures": [fixture], "errors": []},
        "wikidata_entities": {
            "provider": "Wikidata structured data",
            "source_id": "wikidata_entities",
            "license": "CC0-1.0",
            "status": "ok",
            "network_opened": True,
            "venues": [{"fixture_id": fixture["id"], "venue": venue}],
            "errors": [],
        },
    }


def test_wikidata_venue_section_requires_exact_fixture_projection() -> None:
    _validate_live_snapshot(_wikidata_venue_live_section(), AS_OF, {"premier-league"})


def test_wikidata_venue_section_rejects_tampered_fixture_coordinates() -> None:
    live = _wikidata_venue_live_section()
    live["fixture_feed"]["fixtures"][0]["venue"]["latitude"] = 0.0
    with pytest.raises(ValueError, match="Wikidata fixture venue projection"):
        _validate_live_snapshot(live, AS_OF, {"premier-league"})


def test_legacy_roles_are_auditable_but_never_model_admitted(tmp_path: Path) -> None:
    live = {
        "schema_version": "1.0.0",
        "as_of": AS_OF.isoformat(),
        "expected_competitions": [item["id"] for item in _base_snapshot()["competitions"]],
        "roles": EXPECTED_SOURCE_ROLES,
        "espn": {
            "provider": "ESPN",
            "fixtures": [
                {
                    "id": "espn:self-authorized",
                    "competition_id": "premier-league",
                    "kickoff_at": "2026-08-26T18:00:00+00:00",
                    "status": "upcoming",
                }
            ],
            "errors": [],
            "authorization_reference": "self-issued",
            "commercial_reuse_verified_by_code": True,
        },
    }
    live_path = tmp_path / "legacy.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    result = attach_current_data(_base_snapshot(), live_path, now=AS_OF)

    assert result["current_data"]["role_contract"] == {
        "generation": "legacy",
        "model_admission": False,
        "purpose": "research_display_only",
    }
    assert result["current_data"]["model_admission"]["eligible"] is False
    assert not any(row.get("id") == "espn:self-authorized" for row in result["matches"])


def test_real_sync_v260_snapshot_attaches_without_role_schema_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "sync-current.json"
    monkeypatch.setattr(
        "league_platform.sync_live.fetch_openligadb_fixtures",
        lambda *, now: {
            "provider": "OpenLigaDB",
            "retrieved_at": now.isoformat(),
            "status": "unavailable",
            "matches": [],
            "errors": [],
        },
    )

    produced = _sync_unlocked(
        output,
        now=AS_OF,
        openfootball_raw_archive_dir=tmp_path / "volatile-raw",
        allow_empty_snapshot=True,
    )
    attached = attach_current_data(_base_snapshot(), output, now=AS_OF)

    assert produced["roles"] == V260_ROLES
    assert attached["current_data"]["role_contract"]["generation"] == "v260"
    assert attached["current_data"]["model_admission"]["eligible"] is False
