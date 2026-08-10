"""Current-data adapters used only for future fixtures and as-of features."""

from league_platform.live_sources.espn import fetch_espn_fixtures
from league_platform.live_sources.understat import fetch_understat_features

__all__ = ["fetch_espn_fixtures", "fetch_understat_features"]
