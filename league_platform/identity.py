"""Stable platform identity helpers."""

from __future__ import annotations

import re

from unidecode import unidecode


TEAM_ALIASES = {
    "premier-league": {
        "AFC Bournemouth": "Bournemouth",
        "Brighton & Hove Albion": "Brighton",
        "Leeds United": "Leeds",
        "Manchester City": "Man City",
        "Manchester United": "Man United",
        "Newcastle United": "Newcastle",
        "Nottingham Forest": "Nott'm Forest",
        "Tottenham Hotspur": "Tottenham",
    },
    "la-liga": {
        "Alavés": "Alaves",
        "Athletic Club": "Ath Bilbao",
        "Atlético Madrid": "Ath Madrid",
        "Celta Vigo": "Celta",
        "Espanyol": "Espanol",
        "Rayo Vallecano": "Vallecano",
        "Real Betis": "Betis",
        "Real Sociedad": "Sociedad",
    },
    "bundesliga": {
        "1. FC Union Berlin": "Union Berlin",
        "Bayer Leverkusen": "Leverkusen",
        "Borussia Dortmund": "Dortmund",
        "Borussia Mönchengladbach": "M'gladbach",
        "Eintracht Frankfurt": "Ein Frankfurt",
        "FC Augsburg": "Augsburg",
        "FC Cologne": "FC Koln",
        "SC Freiburg": "Freiburg",
        "TSG Hoffenheim": "Hoffenheim",
        "VfB Stuttgart": "Stuttgart",
    },
    "serie-a": {
        "AC Milan": "Milan",
        "AS Roma": "Roma",
        "Internazionale": "Inter",
    },
    "ligue-1": {
        "AJ Auxerre": "Auxerre",
        "AS Monaco": "Monaco",
        "Le Havre AC": "Le Havre",
        "Paris Saint-Germain": "Paris SG",
        "Stade Rennais": "Rennes",
    },
    "csl": {
        "Henan": "Henan FC",
        "Shanghai Port": "Shanghai Port FC",
        "Shenzhen Xinpengcheng": "Shenzhen Peng City",
        "Zhejiang Professional FC": "Zhejiang Professional",
    },
}


def canonical_team_name(league_id: str, name: str) -> str:
    return TEAM_ALIASES.get(league_id, {}).get(name, name)


def team_id(league_id: str, name: str) -> str:
    canonical_name = canonical_team_name(league_id, name)
    slug = re.sub(r"[^a-z0-9]+", "-", unidecode(canonical_name).lower()).strip("-")
    return f"{league_id}:{slug}"
