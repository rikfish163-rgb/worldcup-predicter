from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
import urllib.request

import pytest

from league_platform.live_sources.bundesliga import fetch_bundesliga_lineups
from league_platform.live_sources.cfl_official import fetch_cfl_current
from league_platform.live_sources.five_hundred_league import fetch_league_page
from league_platform.live_sources.fotmob import fetch_fotmob_lineups
from league_platform.live_sources.geocoding import fetch_open_meteo_geocoding
from league_platform.live_sources.laliga import fetch_laliga_lineups
from league_platform.live_sources.lazq import fetch_lazq_matches
from league_platform.live_sources.ligue1 import fetch_ligue1_lineups
from league_platform.live_sources.news import fetch_news
from league_platform.live_sources.oddstorm import (
    fetch_oddstorm_asian_lines,
    fetch_oddstorm_history,
)
from league_platform.live_sources.open_meteo import (
    fetch_open_meteo_forecasts,
    fetch_open_meteo_weather,
)
from league_platform.live_sources.premier_league import (
    fetch_premier_league_fixtures,
    fetch_premier_league_lineups,
)
from league_platform.live_sources.seriea import fetch_serie_a_lineups
from league_platform.live_sources.sofascore import (
    fetch_sofascore_pre_match,
    fetch_sofascore_prematch,
)
from league_platform.live_sources.sports_lottery import fetch_sports_lottery
from league_platform.live_sources.sports_lottery_history import (
    fetch_fixed_bonus_history,
    fetch_uniform_match_result,
)
from league_platform.live_sources.understat import fetch_understat_features
from league_platform.sources.checkbestodds import fetch_checkbestodds_match
from league_platform.sources.football_data_china import fetch_football_data_china
from league_platform.sources.sevenm_csl import fetch_sevenm_csl_season


NOW = datetime(2026, 8, 25, 0, 30, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)
UPCOMING_FIXTURE = {
    "id": "fixture-1",
    "status": "upcoming",
    "kickoff_at": KICKOFF.isoformat(),
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "sofascore_event_id": "12345",
    "latitude": 51.555,
    "longitude": -0.108,
    "venue": {
        "name": "Emirates Stadium",
        "city": "London",
        "country": "England",
        "country_code": "GB",
    },
}


class ExplodingOpener:
    """Expose both supported opener protocols and record every network attempt."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise OSError("network boundary reached")

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise OSError("network boundary reached")


FetchCall = Callable[[ExplodingOpener], dict[str, object]]


UNTRUSTED_AUTHORIZATION = {
    "authorization_reference": "operator-self-issued-permission",
    "operator_registry": {"default": "allow", "network_fetch": True},
    "config": {"rights": "allow", "network_opened": True},
}


def test_descriptive_aliases_share_the_same_rights_gated_entrypoint() -> None:
    assert fetch_open_meteo_forecasts is fetch_open_meteo_weather
    assert fetch_sofascore_pre_match is fetch_sofascore_prematch


def test_oddstorm_rights_gate_precedes_identifier_validation() -> None:
    opener = ExplodingOpener()

    result = fetch_oddstorm_history(
        "operator-invented-id",
        fixture_kickoff_at=KICKOFF,
        now=NOW,
        opener=opener,
        authorization_reference="self-issued",
    )

    assert result["status"] == "rights_blocked"
    assert result["rights"]["ignored_inputs"] == ["authorization_reference"]
    assert result["history"] == []
    assert opener.calls == 0


@pytest.mark.parametrize(
    ("fetch", "source_id", "provider", "empty_fields", "ignored_inputs"),
    [
        pytest.param(
            lambda opener: fetch_understat_features(now=NOW, opener=opener),
            "understat_xg",
            "Understat",
            ("observations",),
            [],
            id="understat",
        ),
        pytest.param(
            lambda opener: fetch_sofascore_prematch([UPCOMING_FIXTURE], now=NOW, opener=opener),
            "sofascore_prematch",
            "SofaScore",
            ("events",),
            [],
            id="sofascore",
        ),
        pytest.param(
            lambda opener: fetch_open_meteo_weather([UPCOMING_FIXTURE], now=NOW, opener=opener),
            "open_meteo_weather",
            "Open-Meteo",
            ("weather",),
            [],
            id="open-meteo-weather",
        ),
        pytest.param(
            lambda opener: fetch_news(now=NOW, opener=opener),
            "public_rss_news",
            "Public RSS",
            ("news",),
            [],
            id="public-rss-news",
        ),
        pytest.param(
            lambda opener: fetch_oddstorm_asian_lines(
                now=NOW,
                opener=opener,
                authorization_reference="self-issued",
            ),
            "oddstorm_market_comparison",
            "OddStorm public bookmaker comparison",
            ("lines",),
            ["authorization_reference"],
            id="oddstorm-current-self-authorized",
        ),
        pytest.param(
            lambda opener: fetch_oddstorm_history(
                "113979855",
                fixture_kickoff_at=KICKOFF,
                now=NOW,
                opener=opener,
                authorization_reference="self-issued",
            ),
            "oddstorm_market_history",
            "OddStorm public bookmaker comparison",
            ("history",),
            ["authorization_reference"],
            id="oddstorm-history-self-authorized",
        ),
        pytest.param(
            lambda opener: fetch_sports_lottery(now=NOW, opener=opener),
            "sports_lottery_official",
            "Sports Lottery",
            ("matches", "fallbacks"),
            [],
            id="sports-lottery",
        ),
        pytest.param(
            lambda opener: fetch_open_meteo_geocoding([UPCOMING_FIXTURE], now=NOW, opener=opener),
            "open_meteo_geocoding",
            "Open-Meteo Geocoding",
            ("geocodes",),
            [],
            id="open-meteo-geocoding",
        ),
        pytest.param(
            lambda opener: fetch_fotmob_lineups(
                [UPCOMING_FIXTURE],
                now=NOW,
                opener=opener,
                allow_robots_disallowed=True,
            ),
            "fotmob_public_api",
            "FotMob",
            ("lineups",),
            ["config"],
            id="fotmob-robots-override",
        ),
    ],
)
def test_blocked_public_fetches_return_before_any_network_boundary(
    fetch: FetchCall,
    source_id: str,
    provider: str,
    empty_fields: tuple[str, ...],
    ignored_inputs: list[str],
) -> None:
    opener = ExplodingOpener()

    result = fetch(opener)

    assert result.get("schema_version") == "matchline.source_rights_result.v1"
    assert result.get("status") == "rights_blocked"
    assert result.get("provider") == provider
    assert result.get("checked_at") == NOW.isoformat()
    assert result.get("retrieved_at") is None
    assert result.get("network_opened") is False
    assert result.get("access_allowed") is False
    assert result.get("model_eligible") is False
    assert result.get("errors") == []
    for field in empty_fields:
        assert result.get(field) == []
    rights = result.get("rights")
    assert isinstance(rights, dict)
    assert rights["policy_version"] == "v260"
    assert rights["source_id"] == source_id
    assert rights["use_case"] == "network_fetch"
    assert rights["decision"] == "block"
    assert rights["ignored_inputs"] == ignored_inputs
    assert opener.calls == 0


@pytest.mark.parametrize(
    ("fetch", "source_id", "provider", "empty_fields"),
    [
        pytest.param(
            lambda opener: fetch_cfl_current(
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "cfl_official_current",
            "Chinese Professional Football League official",
            (
                "fixtures",
                "date_only_fixtures",
                "recent_results",
                "upcoming_3_days",
                "upcoming_7_days",
            ),
            id="cfl-current",
        ),
        pytest.param(
            lambda opener: fetch_premier_league_fixtures(
                object(),
                season=object(),
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "premier_league_public_pages",
            "Premier League official",
            ("fixtures",),
            id="premier-league-fixtures",
        ),
        pytest.param(
            lambda opener: fetch_premier_league_lineups(
                object(),
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "official_premier_league_lineups",
            "Premier League official",
            ("lineups",),
            id="premier-league-lineups",
        ),
        pytest.param(
            lambda opener: fetch_laliga_lineups(
                object(),
                now=object(),
                horizon_hours=object(),
                max_pages=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "official_laliga_lineups",
            "LaLiga official",
            ("fixtures", "lineups"),
            id="laliga-lineups",
        ),
        pytest.param(
            lambda opener: fetch_bundesliga_lineups(
                object(),
                now=object(),
                horizon_hours=object(),
                max_pages=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "official_bundesliga_lineups",
            "Bundesliga official",
            ("fixtures", "lineups"),
            id="bundesliga-lineups",
        ),
        pytest.param(
            lambda opener: fetch_serie_a_lineups(
                object(),
                now=object(),
                horizon_hours=object(),
                max_matches=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "official_serie_a_lineups",
            "Serie A official",
            ("fixtures", "lineups"),
            id="serie-a-lineups",
        ),
        pytest.param(
            lambda opener: fetch_ligue1_lineups(
                object(),
                now=object(),
                horizon_hours=object(),
                max_weeks=object(),
                max_matches=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "official_ligue1_lineups",
            "Ligue 1 official",
            ("fixtures", "lineups"),
            id="ligue1-lineups",
        ),
        pytest.param(
            lambda opener: fetch_league_page(
                object(),
                object(),
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "five_hundred_league_public_pages",
            "500彩票网 public league page",
            ("rows",),
            id="five-hundred-league-page",
        ),
        pytest.param(
            lambda opener: fetch_lazq_matches(
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "lazq_public_mirror",
            "Lazq public mirror",
            ("matches",),
            id="lazq-mirror",
        ),
        pytest.param(
            lambda opener: fetch_uniform_match_result(
                object(),
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "sports_lottery_official",
            "Sports Lottery official historical gateway",
            ("matches",),
            id="sports-lottery-uniform-history",
        ),
        pytest.param(
            lambda opener: fetch_fixed_bonus_history(
                object(),
                now=object(),
                kickoff_at=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "sports_lottery_official",
            "Sports Lottery official historical gateway",
            ("odds_history",),
            id="sports-lottery-fixed-bonus-history",
        ),
        pytest.param(
            lambda opener: fetch_football_data_china(
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "football_data_china_public_csv",
            "football-data.co.uk China",
            ("matches",),
            id="football-data-china",
        ),
        pytest.param(
            lambda opener: fetch_sevenm_csl_season(
                object(),
                now=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "sevenm_csl_public_fixture_script",
            "7M Sports",
            ("matches",),
            id="sevenm-csl",
        ),
        pytest.param(
            lambda opener: fetch_checkbestodds_match(
                object(),
                fixture_id=object(),
                retrieved_at=object(),
                opener=opener,
                **UNTRUSTED_AUTHORIZATION,
            ),
            "checkbestodds_public_pages",
            "CheckBestOdds",
            ("matches", "markets"),
            id="checkbestodds",
        ),
    ],
)
def test_newly_blocked_fetches_fail_closed_before_validation_or_network_construction(
    monkeypatch: pytest.MonkeyPatch,
    fetch: FetchCall,
    source_id: str,
    provider: str,
    empty_fields: tuple[str, ...],
) -> None:
    opener = ExplodingOpener()
    before = datetime.now(timezone.utc)

    def explode_constructor(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("network object constructed before rights gate")

    monkeypatch.setattr(urllib.request, "Request", explode_constructor)
    monkeypatch.setattr(urllib.request, "build_opener", explode_constructor)

    result = fetch(opener)
    after = datetime.now(timezone.utc)

    assert result.get("schema_version") == "matchline.source_rights_result.v1"
    assert result.get("status") == "rights_blocked"
    assert result.get("provider") == provider
    checked_at = datetime.fromisoformat(str(result.get("checked_at")))
    assert before <= checked_at <= after
    assert result.get("retrieved_at") is None
    assert result.get("network_opened") is False
    assert result.get("access_allowed") is False
    assert result.get("commercial_reuse_verified") is False
    assert result.get("model_eligible") is False
    assert result.get("errors") == []
    for field in empty_fields:
        assert result.get(field) == []
    rights = result.get("rights")
    assert isinstance(rights, dict)
    assert rights["policy_version"] == "v260"
    assert rights["source_id"] == source_id
    assert rights["use_case"] == "network_fetch"
    assert rights["decision"] == "block"
    assert rights["ignored_inputs"] == [
        "authorization_reference",
        "config",
        "operator_registry",
    ]
    assert opener.calls == 0
