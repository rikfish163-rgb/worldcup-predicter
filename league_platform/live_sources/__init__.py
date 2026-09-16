"""Current-data adapters used only for future fixtures and as-of features."""

from league_platform.live_sources.espn import fetch_espn_fixtures
from league_platform.live_sources.espn_market import fetch_espn_markets
from league_platform.live_sources.espn_injuries import fetch_espn_injuries
from league_platform.live_sources.espn_roster import (
    DEFAULT_MAX_ROSTER_TEAMS,
    fetch_espn_team_rosters,
)
from league_platform.live_sources.news import fetch_news
from league_platform.live_sources.openfootball_live import fetch_openfootball_current
from league_platform.live_sources.cfl_official import fetch_cfl_current
from league_platform.live_sources.openligadb import fetch_openligadb_fixtures
from league_platform.live_sources.open_meteo import fetch_open_meteo_weather
from league_platform.live_sources.met_norway import fetch_met_norway_weather
from league_platform.live_sources.wikidata import fetch_wikidata_venues
from league_platform.live_sources.oddstorm import (
    fetch_oddstorm_asian_lines,
    fetch_oddstorm_history,
)
from league_platform.live_sources.premier_league import (
    fetch_premier_league_fixtures,
    fetch_premier_league_lineups,
)
from league_platform.live_sources.laliga import fetch_laliga_lineups
from league_platform.live_sources.bundesliga import fetch_bundesliga_lineups
from league_platform.live_sources.seriea import fetch_serie_a_lineups
from league_platform.live_sources.ligue1 import fetch_ligue1_lineups
from league_platform.live_sources.lineup_archive import archive_premier_league_lineups
from league_platform.live_sources.lazq import fetch_lazq_matches
from league_platform.live_sources.sofascore import fetch_sofascore_prematch
from league_platform.live_sources.fotmob import fetch_fotmob_lineups
from league_platform.live_sources.sports_lottery import fetch_sports_lottery
from league_platform.live_sources.sports_lottery_history import (
    fetch_fixed_bonus_history,
    fetch_uniform_match_result,
)
from league_platform.live_sources.understat import fetch_understat_features
from league_platform.live_sources.five_hundred_league import (
    fetch_league_page as fetch_five_hundred_league_page,
)
from league_platform.live_sources.crawl4ai import (
    Crawl4AIConfig,
    Crawl4AIConfigError,
    fetch_crawl4ai,
    load_crawl4ai_configs,
)

__all__ = [
    "fetch_espn_fixtures",
    "fetch_espn_markets",
    "fetch_espn_injuries",
    "fetch_espn_team_rosters",
    "DEFAULT_MAX_ROSTER_TEAMS",
    "fetch_news",
    "fetch_openfootball_current",
    "fetch_cfl_current",
    "fetch_openligadb_fixtures",
    "fetch_open_meteo_weather",
    "fetch_met_norway_weather",
    "fetch_wikidata_venues",
    "fetch_oddstorm_asian_lines",
    "fetch_oddstorm_history",
    "fetch_premier_league_fixtures",
    "fetch_premier_league_lineups",
    "fetch_laliga_lineups",
    "fetch_bundesliga_lineups",
    "fetch_serie_a_lineups",
    "fetch_ligue1_lineups",
    "archive_premier_league_lineups",
    "fetch_lazq_matches",
    "fetch_sofascore_prematch",
    "fetch_fotmob_lineups",
    "fetch_sports_lottery",
    "fetch_fixed_bonus_history",
    "fetch_uniform_match_result",
    "fetch_understat_features",
    "fetch_five_hundred_league_page",
    "Crawl4AIConfig",
    "Crawl4AIConfigError",
    "fetch_crawl4ai",
    "load_crawl4ai_configs",
]
