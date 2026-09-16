from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from league_platform.catalog import LEAGUES, get_league
from league_platform.current import (
    _lottery_competition_id,
    _lottery_name_variants,
    _normalise_join_name,
)
from league_platform.identity import canonical_team_name
from league_platform.live_sources.espn import ESPN_CODES, fetch_espn_fixtures
from league_platform.publish_forecast import COMPETITIONS
from league_platform.sources.match_history import MatchHistorySource
from league_platform.strict_report import EUROPEAN_LEAGUES


def test_championship_is_a_first_class_historical_and_current_competition():
    league = get_league("championship")

    assert league.name_zh == "英冠"
    assert league.match_history_code == "E1"
    assert league.timezone == "Europe/London"
    assert ESPN_CODES[league.id] == "eng.2"
    assert league.id in EUROPEAN_LEAGUES


def test_championship_espn_code_is_known_but_current_fetch_is_rights_blocked():
    urls: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return urls[-1]

        def read(self, _limit):
            return b'{"events": []}'

    def opener(request, timeout):
        assert timeout == 30
        urls.append(request.full_url)
        return Response()

    result = fetch_espn_fixtures(
        now=datetime(2026, 8, 23, tzinfo=timezone.utc),
        opener=opener,
        authorization_reference="test-fixture",
    )

    assert result["status"] == "rights_blocked"
    assert result["errors"] == []
    assert urls == []


def test_championship_lottery_fixture_uses_explicit_competition_and_team_aliases():
    assert _lottery_competition_id("英冠") == "championship"
    row = {
        "home_team": "西布罗姆维奇",
        "away_team": "伯恩利",
    }

    home = _lottery_name_variants(row, "home", "championship")
    away = _lottery_name_variants(row, "away", "championship")

    assert _normalise_join_name("West Bromwich Albion") in home
    assert _normalise_join_name("Burnley") in away
    assert canonical_team_name("championship", "West Bromwich Albion") == "West Brom"


def test_championship_history_is_hash_restored_and_model_ready():
    result = MatchHistorySource(
        Path("data/MatchHistory"),
        now=datetime(2026, 8, 23, tzinfo=timezone.utc),
    ).load("championship")

    assert len(result.matches) == 6072
    assert result.quality["duplicate_fixture_rate"] == 0
    assert result.quality["score_completeness"] == 1
    assert {match.season for match in result.matches} == {
        "1516",
        "1617",
        "1718",
        "1819",
        "1920",
        "2020",
        "2122",
        "2223",
        "2324",
        "2425",
        "2526",
    }


def test_forecast_publication_knows_every_configured_model_competition():
    assert {league.id for league in LEAGUES} <= set(COMPETITIONS)


def test_all_model_leagues_share_the_same_prospective_and_live_result_scope():
    from league_platform.prospective_evaluation import REQUIRED_LEAGUES
    from league_platform.sources.live_results import _COMPETITIONS
    from league_platform.store import _PROSPECTIVE_REQUIRED_LEAGUES

    configured = {league.id for league in LEAGUES}
    assert REQUIRED_LEAGUES == configured
    assert _COMPETITIONS == configured
    assert set(_PROSPECTIVE_REQUIRED_LEAGUES) == configured
