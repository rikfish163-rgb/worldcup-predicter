"""Current-data adapters used only for future fixtures and as-of features."""

from league_platform.live_sources.espn import fetch_espn_fixtures
from league_platform.live_sources.espn_market import fetch_espn_markets
from league_platform.live_sources.news import fetch_news
from league_platform.live_sources.open_meteo import fetch_open_meteo_weather
from league_platform.live_sources.sofascore import fetch_sofascore_prematch
from league_platform.live_sources.understat import fetch_understat_features

__all__ = [
    "fetch_espn_fixtures",
    "fetch_espn_markets",
    "fetch_news",
    "fetch_open_meteo_weather",
    "fetch_sofascore_prematch",
    "fetch_understat_features",
]
