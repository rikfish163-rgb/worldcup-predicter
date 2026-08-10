"""Multi-league football data and prediction platform."""

from .catalog import LEAGUES, League, get_league
from .snapshot import build_platform_snapshot

__all__ = ["LEAGUES", "League", "build_platform_snapshot", "get_league"]
