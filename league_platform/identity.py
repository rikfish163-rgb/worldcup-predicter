"""Stable platform identity helpers."""

from __future__ import annotations

import re

from unidecode import unidecode


def team_id(league_id: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", unidecode(name).lower()).strip("-")
    return f"{league_id}:{slug}"
