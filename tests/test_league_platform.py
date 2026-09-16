from __future__ import annotations

import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from league_platform.app import create_server
from league_platform.catalog import LEAGUES, get_league
from league_platform.current import attach_current_data
from league_platform.current import (
    _build_match_center,
    _espn_injury_feature_for_fixture,
    _lottery_competition_id,
    _lottery_only_fixture,
)
from league_platform.dixon_coles import evaluate_dixon_coles
from league_platform.live_sources.espn import _validate_response_url, parse_espn_payload
from league_platform.live_sources.espn_market import parse_espn_market, parse_espn_team_status
from league_platform.live_sources.understat import (
    aggregate_understat_payload,
    decode_understat_payload,
    fetch_understat_features,
)
from league_platform.identity import canonical_team_name, team_id
from league_platform.future import build_future_predictions
from league_platform.model import evaluate_league
from league_platform.snapshot import build_platform_snapshot
from league_platform.store import PlatformStore
from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource


EXPECTED_LEAGUES = {
    "premier-league",
    "championship",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
    "csl",
}
TEST_SHA256 = "a" * 64
TEST_ROLES = {
    "fixtures_and_results": "ESPN",
    "recent_xg_and_form": "Understat",
    "historical_training": ["football-data.co.uk", "OpenFootball"],
    "current_market": "ESPN event summary / named bookmaker",
    "injuries_and_lineups": None,
}


def _espn_source(as_of: datetime, native_id: str) -> dict:
    return {
        "name": "ESPN",
        "url": "https://site.api.espn.com/example",
        "native_fixture_id": native_id,
        "retrieved_at": as_of.isoformat(),
        "raw_sha256": TEST_SHA256,
    }


def _coverage_fixtures(as_of: datetime, *, exclude: str = "premier-league") -> list[dict]:
    fixtures = []
    for index, competition_id in enumerate(sorted(EXPECTED_LEAGUES - {exclude}), start=10):
        native_id = f"coverage-{index}"
        fixtures.append(
            {
                "id": f"espn:{native_id}",
                "competition_id": competition_id,
                "season": "2026",
                "kickoff_at": "2026-08-22T19:00:00+00:00",
                "home_team": f"Coverage Home {index}",
                "away_team": f"Coverage Away {index}",
                "status": "upcoming",
                "score": None,
                "home_provider_team_id": f"h{index}",
                "away_provider_team_id": f"a{index}",
                "source": _espn_source(as_of, native_id),
            }
        )
    return fixtures


def test_espn_injury_join_requires_both_exact_provider_team_buckets_for_coverage():
    source = {
        "name": "ESPN injury report",
        "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
        "retrieved_at": "2026-08-10T01:00:00+00:00",
        "raw_sha256": TEST_SHA256,
        "policy": {"allow_model": False},
    }
    fixture = {
        "competition_id": "premier-league",
        "home_provider_team_id": "1",
        "away_provider_team_id": "2",
    }
    home = {
        "competition_id": "premier-league",
        "provider_team_id": "1",
        "team_name": "Home",
        "retrieved_at": source["retrieved_at"],
        "reported_player_count": 1,
        "injuries": [
            {
                "provider_player_id": "101",
                "name": "Home Player",
                "status": "Out",
            }
        ],
        "source": source,
    }
    partial = _espn_injury_feature_for_fixture(
        fixture,
        {("premier-league", "1"): home},
    )

    assert partial["coverage_status"] == "partial"
    assert partial["injuries"]["available"] is False
    assert partial["injuries"]["healthy_team_claim"] is False
    assert partial["injuries"]["home"][0]["player_id"] == "101"

    away = {
        **home,
        "provider_team_id": "2",
        "team_name": "Away",
        "reported_player_count": 0,
        "injuries": [],
    }
    complete = _espn_injury_feature_for_fixture(
        fixture,
        {
            ("premier-league", "1"): home,
            ("premier-league", "2"): away,
        },
    )

    assert complete["coverage_status"] == "complete"
    assert complete["injuries"]["available"] is True
    assert complete["injuries"]["away"] == []
    assert complete["injuries"]["healthy_team_claim"] is False


def test_data_manifest_pins_all_model_league_inputs():
    manifest = json.loads(Path("league_platform/data_manifest.json").read_text())

    assert manifest["provider"] == "multiple"
    assert len(manifest["files"]) >= 48
    assert {"E0_1516.csv", "E1_2526.csv", "F1_1920.csv", "CSL_2018.txt", "CSL_2024.txt"} <= {
        item["name"] for item in manifest["files"]
    }
    assert all(item["url"].startswith("https://") for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_sports_lottery_only_fixture_is_explicitly_quarantined():
    item = {
        "match_id": "lot-only-1",
        "match_num": "周三999",
        "league": "英超",
        "home_team": "新球队甲",
        "away_team": "新球队乙",
        "kickoff_at": "2026-08-20T11:00:00+00:00",
        "had_probability": {"h": 0.5, "d": 0.25, "a": 0.25},
        "source": {
            "name": "Sports Lottery",
            "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
            "retrieved_at": "2026-08-12T02:00:00+00:00",
            "raw_sha256": TEST_SHA256,
        },
    }
    assert _lottery_competition_id(item["league"]) == "premier-league"
    fixture = _lottery_only_fixture(item, "premier-league")
    assert fixture["id"] == "sporttery:lot-only-1"
    assert fixture["entity_match_confidence"] < 0.95
    assert fixture["current_features"]["fixture_origin"] == "sports_lottery_only"


def test_espn_current_fixture_contract_preserves_native_ids_and_as_of():
    payload = json.dumps(
        {
            "events": [
                {
                    "id": "401",
                    "date": "2026-08-21T19:00Z",
                    "season": {"year": 2026},
                    "status": {"type": {"name": "STATUS_SCHEDULED"}},
                    "competitions": [
                        {
                            "venue": {
                                "id": "v1",
                                "fullName": "Example Stadium",
                                "address": {
                                    "city": "London",
                                    "country": "England",
                                    "countryCode": "GBR",
                                },
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {"id": "359", "displayName": "Arsenal"},
                                    "score": "0",
                                },
                                {
                                    "homeAway": "away",
                                    "team": {"id": "388", "displayName": "Coventry City"},
                                    "score": "0",
                                },
                            ]
                        }
                    ],
                }
            ]
        }
    ).encode()
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    fixtures = parse_espn_payload(
        payload,
        competition_id="premier-league",
        retrieved_at=as_of,
        url="https://site.api.espn.com/example",
    )

    assert fixtures[0]["id"] == "espn:401"
    assert fixtures[0]["status"] == "upcoming"
    assert fixtures[0]["home_provider_team_id"] == "359"
    assert fixtures[0]["venue"]["city"] == "London"
    assert fixtures[0]["source"]["retrieved_at"] == as_of.isoformat()
    assert len(fixtures[0]["source"]["raw_sha256"]) == 64


def test_espn_finished_fixture_records_90_minute_result_scope():
    payload = json.dumps({
        "events": [{
            "id": "402",
            "date": "2026-08-21T19:00Z",
            "season": {"year": 2026},
            "status": {"type": {"name": "STATUS_FULL_TIME"}},
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"id": "359", "displayName": "Arsenal"}, "score": "2"},
                    {"homeAway": "away", "team": {"id": "388", "displayName": "Coventry City"}, "score": "1"},
                ],
            }],
        }]
    }).encode()
    fixtures = parse_espn_payload(
        payload,
        competition_id="premier-league",
        retrieved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        url="https://site.api.espn.com/example",
    )
    assert fixtures[0]["status"] == "finished"
    assert fixtures[0]["result_scope"] == "regulation_90"
    assert fixtures[0]["score"] == {"home": 2, "away": 1}


def test_espn_market_is_devigged_and_bound_to_native_fixture():
    payload = json.dumps(
        {
            "pickcenter": [
                {
                    "provider": {"name": "ExampleBook"},
                    "homeTeamOdds": {"moneyLine": -150},
                    "drawOdds": {"moneyLine": 300},
                    "awayTeamOdds": {"moneyLine": 400},
                }
            ]
            ,
            "rosters": [
                {
                    "homeAway": "home",
                    "team": {"id": "359", "displayName": "Arsenal"},
                    "roster": [
                        {
                            "starter": True,
                            "athlete": {"id": "p1", "displayName": "Keeper"},
                            "position": {"displayName": "Goalkeeper"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    market = parse_espn_market(
        payload,
        fixture_id="espn:401",
        retrieved_at=as_of,
        url="https://site.api.espn.com/example",
    )

    assert market["fixture_id"] == "espn:401"
    assert market["provider"] == "ExampleBook"
    assert abs(sum(market["probability"].values()) - 1) < 1e-5
    assert market["source"]["raw_sha256"]
    assert market["team_status"]["status"] == "roster_evidence_only"
    assert market["team_status"]["confirmed"] is False
    assert market["team_status"]["teams"]["home"]["players"][0]["name"] == "Keeper"


def test_espn_explicit_pre_kickoff_xi_is_confirmed_but_normal_roster_is_not():
    def roster(side: str) -> dict:
        return {
            "homeAway": side,
            "team": {"id": "home" if side == "home" else "away", "displayName": side.title()},
            "roster": [
                {
                    "starter": True,
                    "jersey": str(index + 1),
                    "athlete": {"id": f"{side}-{index}", "displayName": f"{side}-{index}"},
                    "position": {"displayName": "Midfielder"},
                }
                for index in range(11)
            ]
            + [
                {
                    "starter": False,
                    "athlete": {"id": f"{side}-sub-{index}", "displayName": f"{side}-sub-{index}"},
                    "position": {"displayName": "Substitute"},
                }
                for index in range(5)
            ],
        }

    payload = json.dumps({"rosters": [roster("home"), roster("away")] }).encode()
    observed = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
    status = parse_espn_team_status(
        payload,
        fixture_id="espn:confirmed",
        retrieved_at=observed,
        kickoff_at="2026-08-10T19:00:00+00:00",
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
    )
    assert status["status"] == "confirmed_lineup"
    assert status["confirmed"] is True
    assert status["model_eligible"] is True
    assert status["teams"]["home"]["starter_count"] == 11
    assert status["teams"]["home"]["players"][0]["shirt_number"] == "1"

    after_kickoff = parse_espn_team_status(
        payload,
        fixture_id="espn:confirmed-after",
        retrieved_at=datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc),
        kickoff_at="2026-08-10T19:00:00+00:00",
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
    )
    assert after_kickoff["confirmed"] is True
    assert after_kickoff["model_eligible"] is False


def test_espn_web_summary_host_is_allowlisted_for_public_fallback():
    _validate_response_url(
        "https://site.web.api.espn.com/apis/site/v2/sports/soccer/esp.1/summary?event=1"
    )


def test_espn_untrusted_summary_host_is_rejected():
    with pytest.raises(ValueError):
        _validate_response_url(
            "https://example.com/apis/site/v2/sports/soccer/esp.1/summary?event=1"
        )


def test_espn_market_rejects_non_finite_odds():
    payload = json.dumps(
        {
            "pickcenter": [
                {
                    "homeTeamOdds": {"moneyLine": math.nan},
                    "drawOdds": {"moneyLine": 300},
                    "awayTeamOdds": {"moneyLine": 400},
                }
            ]
        }
    ).encode()

    assert (
        parse_espn_market(
            payload,
            fixture_id="espn:bad-market",
            retrieved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            url="https://site.api.espn.com/example",
        )
        is None
    )


def test_espn_team_status_is_available_without_a_market():
    payload = json.dumps(
        {
            "rosters": [
                {
                    "homeAway": "away",
                    "team": {"id": "2", "displayName": "Away FC"},
                    "roster": [
                        {
                            "active": True,
                            "athlete": {"id": "p2", "displayName": "Midfielder"},
                            "position": {"displayName": "Midfielder"},
                        }
                    ],
                }
            ]
        }
    ).encode()
    status = parse_espn_team_status(
        payload,
        fixture_id="espn:roster-only",
        retrieved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        url="https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
    )
    assert status is not None
    assert status["fixture_id"] == "espn:roster-only"
    assert status["confirmed"] is False
    assert status["teams"]["away"]["players"][0]["status"] == "active_roster"


def test_understat_form_uses_only_results_before_as_of():
    dates = []
    for day in range(1, 8):
        dates.append(
            {
                "id": str(day),
                "datetime": f"2026-05-{day:02d} 15:00:00",
                "isResult": True,
                "h": {"id": "1", "title": "Arsenal"},
                "a": {"id": str(day + 1), "title": f"Team {day}"},
                "xG": {"h": str(day), "a": "1.0"},
                "goals": {"h": str(day % 3), "a": "1"},
            }
        )
    dates.append(
        {
            "id": "future",
            "datetime": "2026-09-01 15:00:00",
            "isResult": True,
            "h": {"id": "1", "title": "Arsenal"},
            "a": {"id": "99", "title": "Future"},
            "xG": {"h": "99", "a": "99"},
            "goals": {"h": "9", "a": "9"},
        }
    )
    as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)

    observations = aggregate_understat_payload(
        json.dumps({"dates": dates}).encode(),
        competition_id="premier-league",
        retrieved_at=as_of,
        url="https://understat.com/example",
    )
    arsenal = next(item for item in observations if item["provider_team_id"] == "1")

    assert arsenal["sample_n"] == 5
    assert arsenal["xg_for"] == 5.0
    assert arsenal["last_match_at"].startswith("2026-05-07")


def test_understat_gzip_payload_can_be_decoded_before_aggregation():
    payload = gzip.compress(json.dumps({"dates": []}).encode())

    assert json.loads(decode_understat_payload(payload))["dates"] == []


def test_understat_gzip_payload_has_decompressed_size_limit():
    oversized = gzip.compress(b"x" * 1025)

    with pytest.raises(ValueError, match="decompressed payload exceeded"):
        decode_understat_payload(oversized, max_bytes=1024)


def test_understat_fetch_is_rights_blocked_before_bootstrap_or_retry():
    class ExplodingOpener:
        def __init__(self):
            self.calls = 0

        def open(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("Understat network boundary must not be reached")

    opener = ExplodingOpener()
    result = fetch_understat_features(
        now=datetime(2026, 8, 10, tzinfo=timezone.utc), opener=opener
    )

    assert result["status"] == "rights_blocked"
    assert result["observations"] == []
    assert result["rights"]["source_id"] == "understat_xg"
    assert result["network_opened"] is False
    assert result["errors"] == []
    assert opener.calls == 0


def test_matchline_rejects_remote_bind_without_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="remote binding requires"):
        create_server("0.0.0.0", 0, tmp_path)


def test_matchline_server_routes_runtime_evidence_separately(monkeypatch, tmp_path):
    captured = {}

    class FakeStore:
        def __init__(self, data_dir, **options):
            captured["data_dir"] = data_dir
            captured.update(options)

    monkeypatch.setattr("league_platform.app.PlatformStore", FakeStore)
    live_path = tmp_path / "runtime" / "current.json"
    model_evidence = tmp_path / "model-evidence"
    runtime_evidence = tmp_path / "runtime-evidence"
    strict_report = runtime_evidence / "strict-backtest-current.json"

    server = create_server(
        "127.0.0.1",
        0,
        tmp_path / "history",
        live_path,
        model_evidence,
        runtime_evidence,
        strict_report,
    )
    try:
        assert captured["data_dir"] == tmp_path / "history"
        assert captured["live_path"] == live_path
        assert captured["evidence_dir"] == model_evidence
        assert captured["runtime_evidence_dir"] == runtime_evidence
        assert captured["strict_report_path"] == strict_report
        assert captured["openfootball_raw_archive_dir"] is None
        assert captured["now"].tzinfo is not None
    finally:
        server.server_close()


def test_catalog_contains_big_five_championship_and_chinese_super_league():
    assert {league.id for league in LEAGUES} == EXPECTED_LEAGUES
    assert get_league("csl").name_zh == "中超"
    assert all(league.timezone for league in LEAGUES)


def test_team_registry_aligns_provider_aliases_without_merging_promoted_clubs():
    assert canonical_team_name("premier-league", "Manchester City") == "Man City"
    assert team_id("premier-league", "Manchester City") == team_id("premier-league", "Man City")
    assert canonical_team_name("csl", "Shanghai Port") == "Shanghai Port FC"
    assert canonical_team_name("premier-league", "Coventry City") == "Coventry"
    assert team_id("premier-league", "Coventry City") == team_id("premier-league", "Coventry")
    assert team_id("la-liga", "Deportivo") == team_id("la-liga", "La Coruna")
    assert team_id("bundesliga", "SC Paderborn 07") == team_id("bundesliga", "Paderborn")
    assert team_id("bundesliga", "Hamburg SV") == team_id("bundesliga", "Hamburg")
    assert team_id("la-liga", "Deportivo") != team_id("la-liga", "Deportivo Alaves")


def test_snapshot_promoted_team_row_cannot_self_admit_without_raw_archive():
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=as_of)
    fixture = {
        "id": "espn:alias-deportivo",
        "competition_id": "la-liga",
        "season": "2627",
        "kickoff_at": "2026-08-21T19:00:00+00:00",
        "home_team": "Deportivo",
        "away_team": "Barcelona",
        "home_team_id": team_id("la-liga", "Deportivo"),
        "away_team_id": team_id("la-liga", "Barcelona"),
        "status": "upcoming",
        "score": None,
        "source": {"name": "ESPN"},
        "current_features": {},
    }
    snapshot["matches"].append(fixture)
    snapshot["current_data"] = {"status": "fresh", "as_of": as_of.isoformat()}

    result = build_future_predictions(snapshot)

    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"


def test_snapshot_unseen_team_row_cannot_self_admit_without_raw_archive():
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=as_of)
    snapshot["matches"].append(
        {
            "id": "espn:cold-start",
            "competition_id": "premier-league",
            "season": "2627",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
            "home_team": "Arsenal",
            "away_team": "Coventry City",
            "home_team_id": team_id("premier-league", "Arsenal"),
            "away_team_id": team_id("premier-league", "Coventry City"),
            "status": "upcoming",
            "score": None,
            "source": {"name": "ESPN"},
            "current_features": {},
        }
    )
    snapshot["current_data"] = {"status": "fresh", "as_of": as_of.isoformat()}

    result = build_future_predictions(snapshot)
    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"


def test_snapshot_horizon_rows_cannot_self_admit_without_raw_archive():
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=as_of)
    common = {
        "competition_id": "premier-league",
        "season": "2627",
        "home_team": "Arsenal",
        "away_team": "Man City",
        "home_team_id": team_id("premier-league", "Arsenal"),
        "away_team_id": team_id("premier-league", "Man City"),
        "status": "upcoming",
        "score": None,
        "source": {"name": "test-fixture"},
        "current_features": {},
    }
    snapshot["matches"].extend(
        [
            {
                **common,
                "id": "inside-seven-days",
                "kickoff_at": "2026-08-17T01:00:00+00:00",
            },
            {
                **common,
                "id": "outside-seven-days",
                "kickoff_at": "2026-08-17T01:00:01+00:00",
            },
        ]
    )
    snapshot["current_data"] = {"status": "fresh", "as_of": as_of.isoformat()}

    result = build_future_predictions(snapshot, prediction_horizon=timedelta(days=7))
    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"


@pytest.mark.parametrize(
    ("competition_id", "fixture_name", "understat_name"),
    [
        ("la-liga", "Atlético Madrid", "Atletico Madrid"),
        ("bundesliga", "RB Leipzig", "RasenBallsport Leipzig"),
        ("bundesliga", "Mainz", "Mainz 05"),
        ("bundesliga", "Borussia Mönchengladbach", "Borussia M.Gladbach"),
        ("bundesliga", "Hamburg SV", "Hamburger SV"),
        ("serie-a", "Parma", "Parma Calcio 1913"),
        ("ligue-1", "Paris Saint-Germain", "Paris Saint Germain"),
    ],
)
def test_understat_provider_titles_share_the_fixture_team_identity(
    competition_id: str, fixture_name: str, understat_name: str
):
    assert team_id(competition_id, fixture_name) == team_id(competition_id, understat_name)


def test_match_history_source_loads_real_cached_big_five_data():
    source = MatchHistorySource(Path("data/MatchHistory"))

    premier_league = source.load("premier-league")

    assert premier_league.status == "stale"
    assert premier_league.matches
    assert premier_league.matches[0].competition_id == "premier-league"
    assert premier_league.matches[0].status == "finished"
    assert premier_league.matches[0].score is not None
    assert premier_league.matches[0].home_team_id.startswith("premier-league:")
    assert len(premier_league.matches[0].source_sha256) == 64
    assert premier_league.matches[0].provider_fixture_id.endswith(tuple(str(n) for n in range(10)))
    assert premier_league.latest_event_at is not None


def test_openfootball_source_loads_expanded_complete_csl_seasons():
    source = OpenFootballSource(Path("data/MatchHistory"))

    csl = source.load("csl")

    assert csl.status == "stale"
    assert len(csl.matches) >= 1500
    assert {match.season for match in csl.matches} >= {"2018", "2019", "2020", "2021", "2022", "2023", "2024"}
    assert all(match.source_name == "OpenFootball" for match in csl.matches)
    assert all(match.source_license_status == "CC0-1.0" for match in csl.matches)
    assert csl.quality["date_only_kickoff_rows"] > 0


def test_openfootball_date_only_rows_are_marked_without_fabricating_time(tmp_path):
    content = """= China | Super League 2024

  Fri Mar 1 2024
    18:00  Team A        v Team B        1-0
           Team C        v Team D        0-0
"""
    (tmp_path / "CSL_2024.txt").write_text(content, encoding="utf-8")

    matches = OpenFootballSource(tmp_path).load("csl").matches

    assert matches[0].kickoff_time_quality == "exact"
    date_only = next(match for match in matches if match.home_team == "Team C")
    assert date_only.kickoff_time_quality == "date_only"
    assert date_only.kickoff_at.hour == 0
    assert date_only.kickoff_at.minute == 0


def test_platform_snapshot_is_source_backed_and_deduplicated():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    snapshot = build_platform_snapshot(Path("data/MatchHistory"), now=now)

    assert snapshot["generated_at"] == now.isoformat()
    assert len(snapshot["competitions"]) == 7
    assert snapshot["summary"]["finished_matches"] >= 23000
    assert snapshot["summary"]["available_competitions"] == 7
    assert snapshot["summary"]["unavailable_competitions"] == 0

    match_ids = [match["id"] for match in snapshot["matches"]]
    assert len(match_ids) == len(set(match_ids))
    assert {match["source"]["name"] for match in snapshot["matches"]} == {
        "football-data.co.uk",
        "OpenFootball",
    }
    assert all(match["source"]["sha256"] for match in snapshot["matches"])
    assert all(match["home_team_id"] and match["away_team_id"] for match in snapshot["matches"])


def test_snapshot_exposes_quality_metrics_instead_of_unqualified_accuracy():
    snapshot = build_platform_snapshot(Path("data/MatchHistory"))

    premier_league = next(
        item for item in snapshot["competitions"] if item["id"] == "premier-league"
    )

    assert premier_league["data_quality"]["duplicate_fixture_rate"] == 0.0
    assert premier_league["data_quality"]["score_completeness"] == 1.0
    assert premier_league["data_quality"]["odds_completeness"] > 0.5
    assert premier_league["model_health"]["status"] == "evaluated"
    assert premier_league["model_health"]["brier_score"] > 0
    assert "accuracy" not in premier_league["model_health"]


def test_store_filters_matches_without_mutating_the_snapshot():
    store = PlatformStore(Path("data/MatchHistory"))

    response = store.matches(competition_id="bundesliga", season="2324", limit=12)

    assert response["count"] == 12
    assert response["total"] == 306
    assert all(match["competition_id"] == "bundesliga" for match in response["matches"])
    assert all(match["season"] == "2324" for match in response["matches"])
    assert store.snapshot()["summary"]["finished_matches"] >= 17000


def test_store_validates_query_contract_and_filters_date():
    store = PlatformStore(Path("data/MatchHistory"))

    dated = store.matches(match_date="2024-05-19", limit=500)

    assert dated["total"] > 0
    assert all(match["kickoff_at"].startswith("2024-05-19") for match in dated["matches"])
    for kwargs in (
        {"competition_id": "unknown"},
        {"status": "unknown"},
        {"match_date": "19-05-2024"},
    ):
        try:
            store.matches(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected validation failure for {kwargs}")


def test_source_rejects_duplicate_fixtures(tmp_path):
    csv = "Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA\n19/05/2024,16:00,A,B,1,0,2,3,4\n"
    (tmp_path / "E0_2324.csv").write_text(csv + csv.split("\n", 1)[1], encoding="utf-8")

    source = MatchHistorySource(tmp_path)
    try:
        source.load("premier-league")
    except ValueError as exc:
        assert "duplicate fixtures" in str(exc)
    else:
        raise AssertionError("duplicate fixture must block the snapshot")


def test_source_marks_missing_kickoff_time_instead_of_filling_noon(tmp_path):
    csv = (
        "Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA\n"
        "19/05/2024,,A,B,1,0,2,3,4\n"
    )
    (tmp_path / "E0_2324.csv").write_text(csv, encoding="utf-8")

    result = MatchHistorySource(tmp_path).load("premier-league")

    assert len(result.matches) == 1
    assert result.matches[0].kickoff_time_quality == "date_only"
    assert result.matches[0].kickoff_at.hour == 0
    assert result.quality["date_only_kickoff_rows"] == 1


def test_source_prefers_closing_average_market_odds(tmp_path):
    csv = (
        "Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA,AvgCH,AvgCD,AvgCA\n"
        "19/05/2024,16:00,A,B,1,0,2,3,4,4,4,2\n"
    )
    (tmp_path / "E0_2324.csv").write_text(csv, encoding="utf-8")

    match = MatchHistorySource(tmp_path).load("premier-league").matches[0]

    assert match.market_probability is not None
    assert match.market_probability.home == pytest.approx(0.25, abs=1e-4)
    assert match.market_probability.draw == pytest.approx(0.25, abs=1e-4)
    assert match.market_probability.away == pytest.approx(0.5, abs=1e-4)
    assert match.market_opening_probability is not None
    assert match.market_opening_probability.home == pytest.approx(1 / 2 / (1 / 2 + 1 / 3 + 1 / 4), abs=1e-4)
    assert match.market_closing_probability is not None
    assert match.market_closing_probability.home == pytest.approx(0.25, abs=1e-4)
    assert match.market_time_basis == "closing_average_pre_kickoff"


def test_source_keeps_opening_and_closing_total_market_odds_separate(tmp_path):
    csv = (
        "Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA,AvgCH,AvgCD,AvgCA,Avg>2.5,Avg<2.5,AvgC>2.5,AvgC<2.5\n"
        "19/05/2024,16:00,A,B,1,0,2,3,4,4,4,2,2,2,3,1.8\n"
    )
    (tmp_path / "E0_2324.csv").write_text(csv, encoding="utf-8")

    match = MatchHistorySource(tmp_path).load("premier-league").matches[0]

    assert match.total_opening_probability == pytest.approx(
        {"over": 0.5, "under": 0.5}, abs=1e-8
    )
    assert match.total_closing_probability is not None
    assert match.total_closing_probability["over"] == pytest.approx(
        (1 / 3) / (1 / 3 + 1 / 1.8), abs=1e-8
    )


def test_walk_forward_evaluation_is_chronological_and_calibrated():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    seasons = sorted({match.season for match in matches})

    evaluation = evaluate_league("premier-league", matches)

    assert evaluation["status"] == "evaluated"
    assert evaluation["calibration_season"] == seasons[-2]
    assert evaluation["evaluation_season"] == seasons[-1]
    assert evaluation["sample_n"] == 380
    assert 0 < evaluation["brier_score"] < 1
    assert 0 < evaluation["log_loss"] < 2
    assert 0 <= evaluation["rps"] < 1
    assert 0 <= evaluation["ece"] < 1
    assert 0 <= evaluation["calibration_alpha"] <= 1
    assert evaluation["prediction_time_rule"] == "pre_kickoff_group_update"
    assert len(evaluation["walk_forward_folds"]) == 4
    assert sum(fold["sample_n"] for fold in evaluation["walk_forward_folds"]) == 380
    assert evaluation["baselines"]["historical_frequency"]["sample_n"] == 380
    assert evaluation["baselines"]["market"]["sample_n"] == 380
    assert evaluation["quality_gate"] == "research_only_underperforms_market"


def test_dixon_coles_candidate_uses_disjoint_calibration_and_holdout():
    matches = MatchHistorySource(Path("data/MatchHistory")).load("premier-league").matches
    seasons = sorted({match.season for match in matches})

    evaluation = evaluate_dixon_coles("premier-league", matches)

    assert evaluation["status"] == "evaluated"
    assert evaluation["model"] == "online_dixon_coles_v1"
    assert evaluation["calibration_season"] == seasons[-2]
    assert evaluation["evaluation_season"] == seasons[-1]
    assert evaluation["sample_n"] == 380
    assert -0.15 <= evaluation["rho"] <= 0.15
    assert 0 < evaluation["brier_score"] < 1


def test_snapshot_exposes_evaluated_model_league_historical_baselines():
    snapshot = build_platform_snapshot(Path("data/MatchHistory"))
    by_id = {item["id"]: item for item in snapshot["competitions"]}

    assert snapshot["summary"]["evaluated_models"] == 7
    assert by_id["premier-league"]["model_health"]["sample_n"] == 380
    assert by_id["championship"]["model_health"]["sample_n"] == 552
    assert by_id["csl"]["model_health"]["status"] == "evaluated"
    assert by_id["csl"]["model_health"]["sample_n"] == 240
    assert by_id["csl"]["model_health"]["selected_candidate"] in {
        "dynamic_elo_three_way_v1",
        "online_dixon_coles_v1",
    }


def test_store_attaches_cutoff_matching_strict_report_without_replacing_future_model_contract(tmp_path):
    strict_report = json.loads(Path("tests/fixtures/evidence/strict-backtest-v16.json").read_text())
    lock = json.loads(Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text())
    strict_report["model_selection_audit"]["prospective_lock"] = lock
    strict_path = tmp_path / "strict-report-current.json"
    strict_path.write_text(json.dumps(strict_report), encoding="utf-8")
    store = PlatformStore(
        Path("data/MatchHistory"),
        live_path=Path("data/live/current.json"),
        strict_report_path=strict_path,
        prospective_lock_path=Path("tests/fixtures/evidence/prospective-model-lock-v259.json"),
    )
    snapshot = store.snapshot()
    premier = next(item for item in snapshot["competitions"] if item["id"] == "premier-league")
    assert premier["strict_model_health"]["model"] == "strict_dynamic_elo_scoreline_v2_causal_rho"
    assert premier["strict_model_health"]["sample_n"] == 2660
    assert premier["strict_model_health"]["time_audit"]["status"] == "pass"
    assert premier["strict_model_health"]["freeze_stages"]["t_minus_90m"]["status"] == "evaluated"
    assert premier["strict_model_health"]["freeze_stages"]["lineup_confirmation"]["status"] == "unavailable"
    delta_ci = premier["strict_model_health"]["market_comparison"]["brier_delta_ci"]
    assert delta_ci["level"] == 0.95
    assert delta_ci["lower"] <= delta_ci["mean"] <= delta_ci["upper"]
    assert snapshot["strict_backtest"]["time_audit"]["premier-league"]["future_leakage_violations"] == 0
    assert snapshot["strict_backtest"]["freeze_stages"]["premier-league"]["lineup_confirmation"]["status"] == "unavailable"
    assert snapshot["strict_backtest"]["overall"]["production_allowed"] is False
    market_reason = next(
        item
        for item in snapshot["prospective_evaluation"]["progress"]["reasons"]
        if item["code"] == "market_gate_not_passed"
    )
    assert market_reason["evidence"] == f"{strict_path}:overall.market_gate"
    predictions = store.predictions()
    assert predictions["status"] == "unavailable"
    assert predictions["predictions"] == []
    assert predictions["reason"] == "verified_openfootball_archive_missing"


def test_store_hides_strict_report_when_prospective_lock_identity_is_stale(tmp_path):
    strict_report = json.loads(
        Path("tests/fixtures/evidence/strict-backtest-v16.json").read_text()
    )
    observed_lock = strict_report["model_selection_audit"]["prospective_lock"]
    observed_lock["model_version_sha256"] = "f" * 64
    observed_lock["freeze_model_name"] = "stale-freeze-model"
    observed_lock["locked_at"] = "2026-08-17T05:26:23+00:00"
    observed_lock["evaluation_window_started_at"] = "2026-08-17T05:26:24+00:00"
    observed_lock["evaluation_window"] = "stale prospective window"
    strict_path = tmp_path / "strict-report-stale.json"
    strict_path.write_text(json.dumps(strict_report), encoding="utf-8")
    store = PlatformStore(
        Path("data/MatchHistory"),
        live_path=Path("data/live/current.json"),
        strict_report_path=strict_path,
    )
    snapshot = store.snapshot()
    premier = next(item for item in snapshot["competitions"] if item["id"] == "premier-league")
    assert "strict_model_health" not in premier
    assert snapshot["strict_backtest"]["status"] == "stale"
    assert snapshot["strict_backtest"]["audit"]["reason"] == "strict_report_prospective_lock_mismatch"
    assert set(snapshot["strict_backtest"]["audit"]["mismatches"]) == {
        "model_version_sha256",
        "freeze_model_name",
        "locked_at",
        "evaluation_window_started_at",
        "evaluation_window",
    }


def test_store_keeps_current_strict_report_when_cutoffs_are_offset_or_a_league_is_unavailable(tmp_path):
    strict_report = json.loads(
        Path("tests/fixtures/evidence/strict-backtest-v16.json").read_text()
    )
    lock = json.loads(
        Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text()
    )
    strict_report["model_selection_audit"]["prospective_lock"] = lock
    # The formal report has no verified CSL history.  The other six cutoffs
    # are numerically identical to the catalog's local-offset timestamps.
    strict_report["leagues"]["csl"]["data_cutoff"] = None
    strict_path = tmp_path / "strict-report-partial.json"
    strict_path.write_text(json.dumps(strict_report), encoding="utf-8")

    store = PlatformStore(
        Path("data/MatchHistory"),
        strict_report_path=strict_path,
        prospective_lock_path=Path("tests/fixtures/evidence/prospective-model-lock-v259.json"),
    )
    snapshot = store.snapshot()

    strict = snapshot["strict_backtest"]
    assert strict["status"] == "partial"
    assert set(strict["covered_competitions"]) == {
        "premier-league",
        "championship",
        "la-liga",
        "bundesliga",
        "serie-a",
        "ligue-1",
    }
    assert strict["missing_competitions"] == ["csl"]
    assert "strict_model_health" in next(
        item for item in snapshot["competitions"] if item["id"] == "premier-league"
    )
    assert "strict_model_health" not in next(
        item for item in snapshot["competitions"] if item["id"] == "csl"
    )


def test_store_exposes_current_prospective_gate_evidence():
    store = PlatformStore(Path("data/MatchHistory"), live_path=Path("data/live/current.json"))
    status = store.snapshot()["prospective_evaluation"]
    assert status["status"] == "pending_prospective_window"
    assert status["model_version_sha256"]
    assert status["pending_n"] >= 1
    assert status["selection_gate_status"] == "blocked_selection_debt"
    assert status["selection_production_eligible"] is False
    next_freezes = status["blocked_diagnostics"]["next_freezes"]
    assert next_freezes
    assert all(item["next_freeze_cutoff_at"] < item["next_kickoff_at"] for item in next_freezes)


def test_store_reads_live_cycle_pointers_from_separate_runtime_evidence_dir(tmp_path):
    model_evidence = tmp_path / "model-evidence"
    runtime_evidence = tmp_path / "runtime-evidence"
    model_evidence.mkdir()
    runtime_evidence.mkdir()

    lock = json.loads(
        Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text(encoding="utf-8")
    )
    (model_evidence / "prospective-model-lock-current.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    (model_evidence / "model-selection-audit-current.json").write_text(
        json.dumps({"gate_status": "blocked_selection_debt", "production_eligible": False}),
        encoding="utf-8",
    )
    # Deliberately stale model-side pointers must not win over the runtime
    # cycle when the caller explicitly supplies a separate runtime root.
    (model_evidence / "prospective-evaluation-current.json").write_text(
        json.dumps({"status": "stale", "scored_n": 99, "pending_n": 99}),
        encoding="utf-8",
    )
    (model_evidence / "prospective-cycle-latest.json").write_text(
        json.dumps({"status": "stale", "capture": {"predictions": 99}}),
        encoding="utf-8",
    )
    (runtime_evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 3,
                "result_conflicts": 0,
                "sample_requirements_met": False,
                "all_required_targets_scored": False,
                "prediction_freezes_verified": False,
            }
        ),
        encoding="utf-8",
    )
    (runtime_evidence / "prospective-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "started_at": "2026-08-24T16:00:23+00:00",
                "finished_at": "2026-08-24T16:02:36+00:00",
                "sync": {"as_of": "2026-08-24T16:02:10+00:00"},
                "capture": {
                    "predictions": 2,
                    "appended": 1,
                    "skipped_duplicate": 1,
                    "blocked": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    store = PlatformStore(
        Path("data/MatchHistory"),
        now=datetime(2026, 8, 24, 16, 3, tzinfo=timezone.utc),
        evidence_dir=model_evidence,
        runtime_evidence_dir=runtime_evidence,
    )
    prospective = store.snapshot()["prospective_evaluation"]

    assert prospective["status"] == "pending_prospective_window"
    assert prospective["pending_n"] == 3
    assert prospective["scored_n"] == 0
    assert prospective["model_version_sha256"] == lock["model_version_sha256"]
    assert prospective["selection_gate_status"] == "blocked_selection_debt"
    assert prospective["progress"]["capture"] == {
        "upcoming_fixtures": 0,
        "predictions_generated": 2,
        "predictions_appended": 1,
        "duplicates_skipped": 1,
        "blocked": 0,
        "lineup_observed": 0,
    }
    assert prospective["progress"]["reasons"][0]["evidence"].startswith(
        str(runtime_evidence)
    )


def test_store_reads_runtime_only_cycle_pointers_when_standard_names_are_absent(tmp_path):
    model_evidence = tmp_path / "model-evidence"
    runtime_evidence = tmp_path / "runtime-evidence"
    model_evidence.mkdir()
    runtime_evidence.mkdir()

    lock = json.loads(
        Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text(encoding="utf-8")
    )
    (model_evidence / "prospective-model-lock-current.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    (model_evidence / "model-selection-audit-current.json").write_text(
        json.dumps({"gate_status": "blocked_selection_debt", "production_eligible": False}),
        encoding="utf-8",
    )
    (runtime_evidence / "runtime-only-evaluation-current.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "scored_n": 0,
                "pending_n": 0,
                "result_conflicts": 0,
                "sample_requirements_met": False,
                "all_required_targets_scored": False,
                "prediction_freezes_verified": False,
            }
        ),
        encoding="utf-8",
    )
    (runtime_evidence / "runtime-only-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "started_at": "2026-08-25T09:26:52+00:00",
                "finished_at": "2026-08-25T09:27:30+00:00",
                "sync": {"as_of": "2026-08-25T09:26:54+00:00"},
                "capture": {
                    "predictions": 0,
                    "appended": 0,
                    "skipped_duplicate": 0,
                    "blocked": 6,
                    "blocked_diagnostics": {
                        "count": 6,
                        "items": [
                            {
                                "competition_id": "premier-league",
                                "next_fixture_id": "openfootball:premier-league:next",
                                "next_kickoff_at": "2026-08-28T19:00:00+00:00",
                                "next_freeze_stage": "t_minus_24h",
                                "next_freeze_cutoff_at": "2026-08-27T19:00:00+00:00",
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    store = PlatformStore(
        Path("data/MatchHistory"),
        now=datetime(2026, 8, 25, 9, 28, tzinfo=timezone.utc),
        evidence_dir=model_evidence,
        runtime_evidence_dir=runtime_evidence,
    )
    prospective = store.snapshot()["prospective_evaluation"]

    assert prospective["status"] == "pending_prospective_window"
    assert prospective["cycle_status"] == "pending_prospective_window"
    assert prospective["blocked_diagnostics"]["count"] == 6
    assert prospective["blocked_diagnostics"]["next_freezes"][0]["next_freeze_stage"] == "t_minus_24h"
    assert prospective["progress"]["capture"]["blocked"] == 6
    assert prospective["progress"]["reasons"][0]["evidence"].endswith(
        "runtime-only-cycle-latest.json:capture.blocked_diagnostics"
    )


def test_store_prefers_newer_runtime_only_failed_cycle_over_older_standard_pointer(tmp_path):
    model_evidence = tmp_path / "model-evidence"
    runtime_evidence = tmp_path / "runtime-evidence"
    model_evidence.mkdir()
    runtime_evidence.mkdir()

    lock = json.loads(
        Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text(
            encoding="utf-8"
        )
    )
    (model_evidence / "prospective-model-lock-current.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    (model_evidence / "model-selection-audit-current.json").write_text(
        json.dumps({"gate_status": "blocked_selection_debt", "production_eligible": False}),
        encoding="utf-8",
    )
    evaluation = {
        "status": "pending_prospective_window",
        "scored_n": 0,
        "pending_n": 1,
        "result_conflicts": 0,
        "sample_requirements_met": False,
        "all_required_targets_scored": False,
        "prediction_freezes_verified": False,
    }
    (runtime_evidence / "prospective-evaluation-current.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    # The normal pointer is an older, valid pending cycle.  The runtime-only
    # pointer is newer and records the actual lock-mismatch failure from the
    # latest execution.  The workbench must not hide that failure behind the
    # older pointer merely because the standard filename exists.
    (runtime_evidence / "prospective-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "pending_prospective_window",
                "started_at": "2026-08-25T09:26:52+00:00",
                "finished_at": "2026-08-25T09:27:30+00:00",
                "sync": {"as_of": "2026-08-25T09:26:54+00:00"},
                "capture": {"predictions": 0, "appended": 0, "blocked": 0},
            }
        ),
        encoding="utf-8",
    )
    (runtime_evidence / "runtime-only-cycle-latest.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "started_at": "2026-08-26T13:20:35.807701+00:00",
                "finished_at": "2026-08-26T13:20:56.330472+00:00",
                "failure": {
                    "stage": "capture",
                    "type": "ValueError",
                    "message": "prospective lock model file hash mismatch",
                },
                "sync": {"as_of": "2026-08-26T13:20:37.508952+00:00"},
                "capture": {"predictions": 0, "appended": 0, "blocked": 0},
            }
        ),
        encoding="utf-8",
    )

    store = PlatformStore(
        Path("data/MatchHistory"),
        now=datetime(2026, 8, 26, 13, 21, tzinfo=timezone.utc),
        evidence_dir=model_evidence,
        runtime_evidence_dir=runtime_evidence,
    )
    prospective = store.snapshot()["prospective_evaluation"]

    assert prospective["cycle_status"] == "failed"
    assert prospective["progress"]["state"] == "cycle_failed"
    assert prospective["progress"]["last_cycle"]["status"] == "failed"
    reason = next(
        item
        for item in prospective["progress"]["reasons"]
        if item["code"] == "prospective_cycle_failed"
    )
    assert reason["blocks_release"] is True
    assert reason["evidence"].endswith("runtime-only-cycle-latest.json:failure")


def test_store_exposes_explicit_prospective_growth_diagnostics():
    store = PlatformStore(Path("data/MatchHistory"), live_path=Path("data/live/current.json"))
    prospective = store.snapshot()["prospective_evaluation"]
    progress = prospective["progress"]

    assert progress["schema_version"] == "matchline.prospective_progress.v1"
    assert progress["phase"] == "prospective_collection"
    assert progress["model"]["parameters_mutable"] is False
    assert progress["window"]["started_at"]
    assert progress["window"]["legacy_model_records"] >= 1
    # A healthy polling cycle may have no eligible freeze cutoff yet.  Zero
    # generated rows is valid in that state; the contract is that counts are
    # numeric and appends can never exceed generated candidates.
    assert isinstance(progress["capture"]["predictions_generated"], int)
    assert progress["capture"]["predictions_generated"] >= progress["capture"]["predictions_appended"] >= 0
    # The evidence ledger is live and may already contain verified results.
    # The progress view must mirror that ledger instead of freezing an old
    # zero-result fixture into the contract.
    assert progress["evaluation"]["scored_n"] == prospective["scored_n"]
    assert progress["evaluation"]["pending_n"] == prospective["pending_n"]
    assert progress["evaluation"]["pending_n"] >= 1
    assert progress["state"] in {"awaiting_first_verified_result", "awaiting_verified_results"}
    assert progress["next_freeze"]["next_freeze_cutoff_at"] < progress["next_freeze"]["next_kickoff_at"]
    reason_codes = {item["code"] for item in progress["reasons"]}
    assert "awaiting_verified_results" in reason_codes
    assert "independent_sample_below_gate" in reason_codes
    # A freshly migrated lock may append its first prediction instead of
    # skipping a duplicate.  When a replay does skip one, the diagnostic must
    # be present; the progress contract should not require a historical
    # duplicate just to pass.
    if progress["capture"]["duplicates_skipped"] > 0:
        assert "idempotent_duplicate_lock_keys" in reason_codes
    else:
        assert "idempotent_duplicate_lock_keys" not in reason_codes
    assert "prospective_window_restarted_by_lock_migration" in reason_codes


def test_model_evaluations_api_prefers_cutoff_matching_strict_report(tmp_path):
    strict_report = json.loads(
        Path("tests/fixtures/evidence/strict-backtest-v16.json").read_text()
    )
    strict_report["model_selection_audit"]["prospective_lock"] = json.loads(
        Path("tests/fixtures/evidence/prospective-model-lock-v259.json").read_text()
    )
    strict_path = tmp_path / "strict-report-current.json"
    strict_path.write_text(json.dumps(strict_report), encoding="utf-8")
    store = PlatformStore(
        Path("data/MatchHistory"),
        strict_report_path=strict_path,
        prospective_lock_path=Path("tests/fixtures/evidence/prospective-model-lock-v259.json"),
    )
    evaluations = store.model_evaluations()
    premier = next(item for item in evaluations if item["competition_id"] == "premier-league")
    assert premier["model"] == "strict_dynamic_elo_scoreline_v2_causal_rho"
    assert premier["sample_n"] == 2660


def test_store_health_exposes_source_and_model_gates():
    store = PlatformStore(Path("data/MatchHistory"))

    health = store.health()

    assert health["status"] == "degraded"
    assert health["sources"]["available"] == 7
    assert health["sources"]["fresh"] == 0
    assert health["sources"]["stale"] == 7
    assert health["sources"]["unavailable"] == 0
    assert health["models"]["evaluated"] == 7
    assert health["models"]["gate"] == "historical_baselines_evaluated_current_predictions_blocked"


def test_current_snapshot_is_attached_with_as_of_and_feature_coverage(tmp_path):
    # The old fixture below exercises the superseded ESPN/Understat contract.
    # Keep the test name for compatibility, but make its assertions follow the
    # v260 provider-neutral read model: OpenFootball is display-only until the
    # consumer replays its durable raw archive, while every unlicensed
    # ancillary section remains quarantined.
    from tests.test_current_v260_readmodel import AS_OF, _base_snapshot, _v260_live

    live_path = tmp_path / "current-v260.json"
    live_path.write_text(json.dumps(_v260_live()), encoding="utf-8")
    snapshot = attach_current_data(_base_snapshot(), live_path, now=AS_OF)
    assert snapshot["current_data"]["status"] == "research_only"
    assert snapshot["current_data"]["as_of"] == AS_OF.isoformat()
    assert snapshot["current_data"]["model_admission"] == {
        "eligible": False,
        "reason": "consumer_raw_replay_required",
        "status": "blocked",
    }
    assert [row["id"] for row in snapshot["matches"]] == [
        "openfootball:premier-league:fixture-one"
    ]
    assert snapshot["openligadb"]["display_lane"] == "isolated_current_only"
    assert snapshot["openligadb"]["redistribution_allowed"] is False
    assert not any(
        key in snapshot
        for key in (
            "understat",
            "espn_markets",
            "espn_injuries",
            "weather",
            "sofascore",
            "sports_lottery",
            "oddstorm",
        )
    )
    future = build_future_predictions(snapshot)
    assert future["status"] == "unavailable"
    assert future["predictions"] == []
    return

    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "authorization_reference": "test-fixture-only",
            "commercial_reuse_verified_by_code": True,
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:1",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "1"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {
            "provider": "Understat",
            "errors": [],
            "team_features": [
                {
                    "competition_id": "premier-league",
                    "team": "Manchester City",
                    "sample_n": 5,
                    "xg_for": 1.5,
                    "xg_against": 1.0,
                    "source": {
                        "name": "Understat",
                        "url": "https://understat.com/example",
                        "retrieved_at": as_of.isoformat(),
                        "wire_sha256": TEST_SHA256,
                        "content_sha256": TEST_SHA256,
                    },
                }
            ],
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
            "team_status": [
                {
                    "fixture_id": "espn:1",
                    "retrieved_at": as_of.isoformat(),
                    "provider": "ESPN event summary",
                    "confirmed": False,
                    "model_eligible": False,
                    "status": "roster_evidence_only",
                    "teams": {
                        "home": {
                            "provider_team_id": "1",
                            "name": "Manchester City",
                            "players": [
                                {
                                    "player_id": "p1",
                                    "name": "Keeper",
                                    "position": "Goalkeeper",
                                    "status": "active_roster",
                                }
                            ],
                            "full_roster": False,
                        }
                    },
                    "source": {
                        "name": "ESPN event summary",
                        "url": "https://site.api.espn.com/example",
                        "raw_sha256": TEST_SHA256,
                    },
                    "note": "roster evidence",
                }
            ],
        },
        "espn_injuries": {
            "provider": "ESPN injury report",
            "status": "fresh",
            "retrieved_at": as_of.isoformat(),
            "reports": [
                {
                    "competition_id": "premier-league",
                    "provider_team_id": "1",
                    "team_name": "Manchester City",
                    "report_status": "published",
                    "injuries": [
                        {
                            "provider_player_id": "101",
                            "name": "Unavailable Player",
                            "status": "Out",
                            "status_type": "injury_report",
                        }
                    ],
                    "reported_player_count": 1,
                    "scheduled_fixture_ids": ["espn:1"],
                    "retrieved_at": as_of.isoformat(),
                    "model_eligible": False,
                    "enters_model": False,
                    "source": {
                        "name": "ESPN injury report",
                        "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": TEST_SHA256,
                        "policy": {"allow_model": False},
                    },
                }
            ],
            "competition_reports": [],
            "errors": [],
            "model_eligible": False,
            "enters_model": False,
            "coverage_semantics": "missing_team_bucket_is_unknown_not_healthy",
        },
        "geocoding": {
            "provider": "Open-Meteo Geocoding",
            "errors": [],
            "geocodes": [
                {
                    "fixture_id": "espn:1",
                    "city": "London",
                    "latitude": 51.5074,
                    "longitude": -0.1278,
                    "source": {
                        "name": "Open-Meteo Geocoding",
                        "url": "https://geocoding-api.open-meteo.com/v1/search?name=London",
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": TEST_SHA256,
                    },
                }
            ],
        },
        "openligadb": {
            "provider": "OpenLigaDB",
            "season": 2026,
            "retrieved_at": as_of.isoformat(),
            "errors": [],
            "matches": [
                {
                    "id": "openligadb:1",
                    "provider_match_id": "1",
                    "competition_id": "bundesliga",
                    "season": "2026",
                    "kickoff_at": "2026-08-28T18:30:00+00:00",
                    "home_team": "Bayern",
                    "away_team": "Stuttgart",
                    "status": "upcoming",
                    "score": None,
                    "source": {
                        "name": "OpenLigaDB",
                        "url": "https://api.openligadb.de/getmatchdata/bl1/2026",
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": TEST_SHA256,
                        "license": "ODbL-1.0",
                        "license_url": "https://www.openligadb.de/lizenz",
                        "attribution_required": True,
                    },
                }
            ],
        },
        "sports_lottery": {
            "provider": "Sports Lottery",
            "retrieved_at": as_of.isoformat(),
            "errors": [],
            "matches": [
                {
                    "match_id": "lot-only-1",
                    "match_num": "周三999",
                    "league": "英超",
                    "home_team": "Lottery Home",
                    "away_team": "Lottery Away",
                    "kickoff_at": "2026-08-20T11:00:00+00:00",
                    "had_probability": {"h": 0.5, "d": 0.25, "a": 0.25},
                    "crs_probability": {},
                    "source": {
                        "name": "Sports Lottery",
                        "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": TEST_SHA256,
                    },
                }
            ],
        },
        "oddstorm": {
            "provider": "OddStorm public bookmaker comparison",
            "retrieved_at": as_of.isoformat(),
            "request_url": "https://www.oddstorm.com/asianodds/league/2182861-china-chinese-super-league",
            "lines": [
                {
                    "match_id": "odd-1",
                    "kickoff_at": "2026-08-22T11:00:00+00:00",
                    "home_team": "Independent Home",
                    "away_team": "Independent Away",
                    "handicap": {"line": -0.5, "home_odds": 1.9, "away_odds": 2.0},
                    "total": {"line": 2.5, "over_odds": 1.9, "under_odds": 1.9},
                    "source": {
                        "name": "OddStorm public bookmaker comparison",
                        "url": "https://www.oddstorm.com/asianodds/league/2182861-china-chinese-super-league",
                        "retrieved_at": as_of.isoformat(),
                        "raw_sha256": TEST_SHA256,
                    },
                }
            ],
            "errors": [],
            "status": "available",
        },
        "premier_league_official": {
            "provider": "Premier League official",
            "season": "2026",
            "matchweeks": [1],
            "retrieved_at": as_of.isoformat(),
            "status": "ok",
            "errors": [],
            "fixtures": [{
                "id": "premierleague:2645195",
                "competition_id": "premier-league",
                "season": "2026",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
                "home_team": "Manchester City",
                "away_team": "Arsenal",
                "status": "upcoming",
                "source": {
                    "name": "Premier League official",
                    "url": "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1/competitions/8/seasons/2026/matchweeks/1/matches",
                    "retrieved_at": as_of.isoformat(),
                    "raw_sha256": TEST_SHA256,
                },
            }],
            "lineups": [{
                "match_id": "2645195",
                "fixture_id": "premierleague:2645195",
                "lineups": {"confirmed": False, "model_eligible": False},
                "source": {
                    "name": "Premier League official",
                    "url": "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v3/matches/2645195/lineups",
                    "retrieved_at": as_of.isoformat(),
                    "raw_sha256": TEST_SHA256,
                },
            }],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")

    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        live_path,
        now=as_of,
    )
    fixture = next(item for item in snapshot["matches"] if item["id"] == "espn:1")

    assert snapshot["current_data"]["status"] == "fresh"
    assert snapshot["current_data"]["geocoding_status"] == "fresh"
    assert snapshot["current_data"]["geocoding_count"] == 1
    assert snapshot["current_data"]["openligadb_status"] == "fresh"
    assert snapshot["current_data"]["openligadb_count"] == 1
    assert snapshot["current_data"]["oddstorm_status"] == "rights_blocked"
    assert snapshot["current_data"]["oddstorm_line_count"] == 0
    assert snapshot["current_data"]["premier_league_official_status"] == "ok"
    assert snapshot["current_data"]["premier_league_official_fixture_count"] == 1
    assert snapshot["current_data"]["premier_league_official_lineup_count"] == 1
    assert snapshot["summary"]["current_fixture_count"] == 8
    assert snapshot["current_data"]["lottery_only_fixture_count"] == 1
    sales_row = snapshot["current_data"]["lottery_sales_schedule"][0]
    assert sales_row["link_status"] == "display_only_low_confidence"
    assert sales_row["link_reason"] == "low_entity_match_confidence"
    assert sales_row["fixture_id"] == "sporttery:lot-only-1"
    assert fixture["home_team_id"] == "premier-league:man-city"
    assert fixture["current_features"]["home"]["sample_n"] == 5
    assert fixture["current_features"]["team_status"]["status"] == "roster_evidence_only"
    assert fixture["current_features"]["espn_injuries"]["coverage_status"] == "partial"
    assert fixture["current_features"]["espn_injuries"]["injuries"]["available"] is False
    assert snapshot["current_data"]["espn_injury_report_count"] == 1
    assert snapshot["current_data"]["espn_injury_partial_fixture_count"] == 1
    assert snapshot["current_data"]["espn_injury_complete_fixture_count"] == 0
    assert fixture["current_features"]["official_lineup"]["canonical_join_status"] == "strict_kickoff_and_team_pair"
    assert snapshot["sports_lottery"]["matches"][0]["crs_probability"] == {}
    assert snapshot["oddstorm"]["lines"] == []
    assert snapshot["oddstorm"]["network_opened"] is False

    future = build_future_predictions(snapshot)
    assert future["status"] == "unavailable"
    assert future["predictions"] == []
    assert future["reason"] == "verified_openfootball_invalid_observed_before"

    linked_live = json.loads(json.dumps(live))
    linked_lottery = linked_live["sports_lottery"]["matches"][0]
    linked_lottery.update(
        {
            "match_id": "lot-linked-1",
            "home_team": "Manchester City",
            "away_team": "Arsenal",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
        }
    )
    linked_path = tmp_path / "current-linked-lottery.json"
    linked_path.write_text(json.dumps(linked_live), encoding="utf-8")
    linked_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), linked_path, now=as_of
    )
    linked_fixture = next(item for item in linked_snapshot["matches"] if item["id"] == "espn:1")
    assert linked_fixture["current_features"]["lottery_market"]["provider"] == "Sports Lottery"
    assert linked_snapshot["current_data"]["lottery_only_fixture_count"] == 0

    shifted_live = json.loads(json.dumps(linked_live))
    shifted_live["sports_lottery"]["matches"][0]["kickoff_at"] = "2026-08-21T20:00:00+00:00"
    shifted_path = tmp_path / "current-shifted-lottery.json"
    shifted_path.write_text(json.dumps(shifted_live), encoding="utf-8")
    shifted_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), shifted_path, now=as_of
    )
    shifted_fixture = next(item for item in shifted_snapshot["matches"] if item["id"] == "espn:1")
    assert shifted_fixture["current_features"]["lottery_market"] is None
    assert shifted_snapshot["current_data"]["lottery_sales_schedule"][0]["link_status"] == "display_only_low_confidence"

    unknown_live = json.loads(json.dumps(live))
    unknown_live["sports_lottery"]["matches"][0]["league"] = "欧超杯"
    unknown_path = tmp_path / "current-unknown-lottery-league.json"
    unknown_path.write_text(json.dumps(unknown_live), encoding="utf-8")
    unknown_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), unknown_path, now=as_of
    )
    unknown_row = unknown_snapshot["current_data"]["lottery_sales_schedule"][0]
    assert unknown_row["link_status"] == "quarantined"
    assert unknown_row["link_reason"] == "unknown_competition_label"
    assert unknown_row["entity_match_confidence"] == 0.0
    assert unknown_snapshot["current_data"]["lottery_only_fixture_count"] == 0

    cross_league_live = json.loads(json.dumps(live))
    # Same date and teams, but a different known competition: it must not
    # attach this market to the ESPN fixture merely because names collide.
    cross_league_live["sports_lottery"]["matches"][0]["league"] = "西甲"
    cross_league_path = tmp_path / "current-cross-league-lottery.json"
    cross_league_path.write_text(json.dumps(cross_league_live), encoding="utf-8")
    cross_league_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), cross_league_path, now=as_of
    )
    cross_row = cross_league_snapshot["current_data"]["lottery_sales_schedule"][0]
    assert cross_row["link_status"] == "display_only_low_confidence"
    assert cross_row["link_reason"] == "low_entity_match_confidence"
    cross_fixture = next(item for item in cross_league_snapshot["matches"] if item["id"] == "espn:1")
    assert cross_fixture["current_features"]["lottery_market"] is None

    web_fallback_live = json.loads(json.dumps(live))
    for row in web_fallback_live["espn"]["fixtures"]:
        row["source"]["url"] = row["source"]["url"].replace(
            "site.api.espn.com", "site.web.api.espn.com"
        )
    for row in web_fallback_live["espn_markets"]["team_status"]:
        row["source"]["url"] = row["source"]["url"].replace(
            "site.api.espn.com", "site.web.api.espn.com"
        )
    web_fallback_path = tmp_path / "current-web-fallback.json"
    web_fallback_path.write_text(json.dumps(web_fallback_live), encoding="utf-8")
    web_fallback_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        web_fallback_path,
        now=as_of,
    )
    assert web_fallback_snapshot["current_data"]["status"] == "fresh"
    predictions = build_future_predictions(snapshot)
    assert predictions["status"] == "unavailable"
    assert predictions["predictions"] == []
    assert predictions["blocked"] == []

    store = PlatformStore(Path("data/MatchHistory"), now=as_of, live_path=live_path)
    store._clock = lambda: as_of + timedelta(hours=7)
    assert store.predictions()["status"] == "unavailable"
    assert store.health()["current_data"]["status"] == "stale"

    valid_live = json.loads(json.dumps(live))
    live["espn"]["fixtures"] = []
    live["espn_markets"]["team_status"] = []
    live["espn_injuries"]["reports"] = []
    live["geocoding"]["geocodes"] = []
    live_path.write_text(json.dumps(live), encoding="utf-8")
    empty_snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    assert empty_snapshot["current_data"]["status"] == "degraded"
    assert empty_snapshot["current_data"]["lottery_only_fixture_count"] == 1

    live = json.loads(json.dumps(valid_live))
    del live["espn"]["fixtures"][0]["source"]["raw_sha256"]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")),
            live_path,
            now=as_of,
        )

    live = json.loads(json.dumps(valid_live))
    del live["understat"]["team_features"][0]["source"]["wire_sha256"]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["roles"]["injuries_and_lineups"] = "unverified-feed"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="roles"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["espn"]["fixtures"][0]["source"]["url"] = "https://evil.example/not-espn"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ValueError, match="allowlisted"):
        attach_current_data(
            build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
        )

    live = json.loads(json.dumps(valid_live))
    live["espn"]["errors"] = [{"competition_id": "premier-league", "error": "offline"}]
    live_path.write_text(json.dumps(live), encoding="utf-8")
    degraded = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    assert degraded["current_data"]["status"] == "degraded"

def test_future_predictions_only_use_post_as_of_fixtures_and_disclose_gates(tmp_path):
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "authorization_reference": "test-fixture-only",
            "commercial_reuse_verified_by_code": True,
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:future",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "future"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )

    result = build_future_predictions(snapshot)

    assert result["status"] == "unavailable"
    assert result["current_data_status"] == snapshot["current_data"]["status"]
    assert result["predictions"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"

    degraded_snapshot = json.loads(json.dumps(snapshot))
    degraded_snapshot["current_data"]["status"] = "degraded"
    degraded_result = build_future_predictions(degraded_snapshot)
    assert degraded_result["status"] == "unavailable"
    assert degraded_result["current_data_status"] == "degraded"
    assert degraded_result["predictions"] == []


def test_future_predictions_block_model_parameters_fitted_after_as_of(tmp_path):
    as_of = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": sorted(EXPECTED_LEAGUES),
        "roles": TEST_ROLES,
        "espn": {
            "provider": "ESPN",
            "authorization_reference": "test-fixture-only",
            "commercial_reuse_verified_by_code": True,
            "errors": [],
            "fixtures": [
                {
                    "id": "espn:future-leak-check",
                    "competition_id": "premier-league",
                    "season": "2026",
                    "kickoff_at": "2026-08-21T19:00:00+00:00",
                    "home_team": "Manchester City",
                    "away_team": "Arsenal",
                    "status": "upcoming",
                    "score": None,
                    "home_provider_team_id": "1",
                    "away_provider_team_id": "2",
                    "source": _espn_source(as_of, "future-leak-check"),
                },
                *_coverage_fixtures(as_of),
            ],
        },
        "understat": {"provider": "Understat", "errors": [], "team_features": []},
        "espn_markets": {
            "provider": "ESPN event summary",
            "errors": [],
            "markets": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live), encoding="utf-8")
    snapshot = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")), live_path, now=as_of
    )
    competition = next(item for item in snapshot["competitions"] if item["id"] == "premier-league")
    competition["model_health"]["data_cutoff"] = "2026-08-11T00:00:00+00:00"

    result = build_future_predictions(snapshot)

    assert result["status"] == "unavailable"
    assert result["predictions"] == []
    assert result["blocked"] == []
    assert result["reason"] == "verified_openfootball_invalid_observed_before"


def test_match_center_keeps_live_and_recent_events_out_of_features():
    as_of = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "id": "espn:live-1",
            "competition_id": "premier-league",
            "kickoff_at": "2026-08-17T11:00:00+00:00",
            "home_team": "Home",
            "away_team": "Away",
            "score": {"home": 1, "away": 0},
        },
        {
            "id": "espn:finished-1",
            "competition_id": "premier-league",
            "kickoff_at": "2026-08-17T10:00:00+00:00",
            "home_team": "Old Home",
            "away_team": "Old Away",
            "score": {"home": 2, "away": 2},
        },
    ]
    center = _build_match_center(
        {
            "espn_markets": {
                "retrieved_at": as_of.isoformat(),
                "fixture_updates": [
                    {
                        "fixture_id": "espn:live-1",
                        "status": "live",
                        "score": {"home": 1, "away": 0},
                        "clock": "67:14",
                    },
                    {
                        "fixture_id": "espn:finished-1",
                        "status": "finished",
                        "score": {"home": 2, "away": 2},
                    },
                ],
                "incidents": [
                    {
                        "fixture_id": "espn:live-1",
                        "events": [{"type": "goal", "minute": "12'"}],
                        "source": {
                            "name": "ESPN event summary",
                            "retrieved_at": as_of.isoformat(),
                            "raw_sha256": "a" * 64,
                        },
                        "enters_model": False,
                    }
                ],
                "match_stats": [],
            }
        },
        fixtures,
        as_of=as_of,
    )
    assert center["status"] == "live"
    assert center["live_match_count"] == 1
    assert center["live"][0]["clock"] == "67:14"
    assert center["live"][0]["enters_model"] is False
    assert center["live"][0]["source"]["raw_sha256"] == "a" * 64
    assert center["recent_finished"] == []
