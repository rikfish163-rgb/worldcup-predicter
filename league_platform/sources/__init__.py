"""Source adapters for normalized league data."""

from .checkbestodds import (
    fetch_checkbestodds_match,
    parse_checkbestodds_match_page,
    parse_checkbestodds_more_odds,
)
from .match_history import MatchHistorySource
from .live_results import load_live_results
from .espn_kickoff import apply_kickoff_enrichment, load_kickoff_enrichment
from .sevenm_csl import (
    fetch_sevenm_csl_season,
    parse_sevenm_csl_fixture_script,
    season_url as sevenm_csl_season_url,
)

from league_platform.sources.football_data_china import (
    fetch_football_data_china,
    parse_football_data_china_csv,
)

__all__ = [
    "MatchHistorySource",
    "load_live_results",
    "fetch_checkbestodds_match",
    "parse_checkbestodds_match_page",
    "parse_checkbestodds_more_odds",
    "fetch_football_data_china",
    "parse_football_data_china_csv",
    "apply_kickoff_enrichment",
    "load_kickoff_enrichment",
    "fetch_sevenm_csl_season",
    "parse_sevenm_csl_fixture_script",
    "sevenm_csl_season_url",
]
