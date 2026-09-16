"""Causal adapter for OddStorm's public Asian-odds pages.

OddStorm is an independent public bookmaker-comparison source, not the
official China Sports Lottery.  The current page is useful for future market
snapshots and exposes per-side odds identifiers whose public history endpoint
contains provider timestamps.

The crucial causal rule is that provider history is never treated as though
Matchline observed it in the past.  Every record keeps both ``effective_at``
(the provider timestamp) and ``observed_at`` (our first retrieval).  A freeze
may use the record only after both timestamps and before kickoff.
"""

from __future__ import annotations

import hashlib
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_envelope


ODDSTORM_HOST = "www.oddstorm.com"
ODDSTORM_CSL_LEAGUE_ID = "2182861"
ODDSTORM_CSL_ASIAN_URL = (
    "https://www.oddstorm.com/asianodds/league/"
    "2182861-china-chinese-super-league"
)
ODDSTORM_HISTORY_URL = "https://www.oddstorm.com/odds/oddhistory"
ODDSTORM_TERMS_URL = "https://www.oddstorm.com/terms"
ODDSTORM_RIGHTS_STATUS = "blocked_pending_express_written_permission"
SOURCE_NAME = "OddStorm public bookmaker comparison"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
EMPTY_PAGE_RETRY_COUNT = 1
# OddStorm's public clock is labelled ``UKT`` and exposes explicit ``UKT
# +/-N`` selectors.  The unqualified value is the fixed UTC base; applying
# Europe/London here would incorrectly subtract one hour during British
# Summer Time (and would make the provider time disagree with the fixture
# feed).  Keep the name explicit so the civil-time distinction is visible at
# every parse site.
_UKT_TZ = timezone.utc
_DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
    r"(\d{1,2}) "
    r"(January|February|March|April|May|June|July|August|September|October|November|December) "
    r"(\d{4})(?: UKT)?$"
)
_HISTORY_DATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([01]\d|2[0-3]):([0-5]\d)$"
)
_RELATIVE_HISTORY_RE = re.compile(
    r"^(?:(\d+)\s+days?\s+)?"
    r"(?:(\d+)\s+(?:hrs?|hours?)\s+)?"
    r"(?:(\d+)\s+mins?\s+)?"
    r"(?:(\d+)\s+secs?\s+)?ago$",
    re.IGNORECASE,
)
_MONTHS_LONG = {
    name: index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
_MONTHS_SHORT = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}




def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _reference_time(value: datetime | None) -> datetime:
    return _utc(value or datetime.now(timezone.utc), field="retrieved_at")


def _validate_page_url(url: str) -> None:
    parsed = urlparse(url)
    path_pattern = rf"^/asianodds/league/{ODDSTORM_CSL_LEAGUE_ID}(?:-[a-z0-9-]+)?$"
    if (
        parsed.scheme != "https"
        or parsed.hostname != ODDSTORM_HOST
        or parsed.port not in (None, 443)
        or not re.fullmatch(path_pattern, parsed.path)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OddStorm Asian-odds URL is not allowlisted")


def _validate_history_url(url: str, *, odds_id: str | None = None) -> None:
    parsed = urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ODDSTORM_HOST
        or parsed.port not in (None, 443)
        or parsed.path != "/odds/oddhistory"
        or parsed.params
        or parsed.fragment
        or set(query) != {"id"}
        or len(query["id"]) != 1
        or not query["id"][0].isdigit()
        or (odds_id is not None and query["id"][0] != odds_id)
    ):
        raise ValueError("OddStorm history URL is not allowlisted")


def _number(value: object, *, minimum: float | None = None) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return None
    return result


def _devig_pair(first: float | None, second: float | None, labels: tuple[str, str]) -> dict[str, float] | None:
    if first is None or second is None or first <= 1 or second <= 1:
        return None
    inverse_first = 1.0 / first
    inverse_second = 1.0 / second
    total = inverse_first + inverse_second
    if not math.isfinite(total) or total <= 0:
        return None
    return {
        labels[0]: round(inverse_first / total, 8),
        labels[1]: round(inverse_second / total, 8),
    }


def _source(*, url: str, payload: bytes, retrieved_at: datetime) -> dict[str, Any]:
    return {
        "name": SOURCE_NAME,
        "url": url,
        "source_level": "public_market_comparison",
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "official_sports_lottery": False,
        "purchase_eligible": False,
    }


class _AsianPageParser(HTMLParser):
    """Small purpose-built parser; no script execution and no DOM dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_date: str | None = None
        self.rows: list[dict[str, Any]] = []
        self._group_date_parts: list[str] | None = None
        self._row: dict[str, Any] | None = None
        self._date_row = False
        self._cell: dict[str, Any] | None = None
        self._date_row_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        classes = set(values.get("class", "").split())
        if tag == "span" and "od-group-date" in classes:
            self._group_date_parts = []
        elif tag == "tr":
            self._date_row = "od-daterow" in classes
            self._date_row_parts = []
            match_id = values.get("data-mid", "").strip()
            self._row = {"match_id": match_id, "cells": []} if match_id else None
        elif tag == "td" and (self._row is not None or self._date_row):
            self._cell = {
                "class": classes,
                "field": values.get("data-f", "").strip(),
                "odds_id": values.get("data-oid", "").strip(),
                "text": [],
                "href": None,
            }
        elif tag == "a" and self._cell is not None:
            self._cell["href"] = values.get("href")

    def handle_data(self, data: str) -> None:
        if self._group_date_parts is not None:
            self._group_date_parts.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._group_date_parts is not None:
            value = " ".join("".join(self._group_date_parts).split())
            if value:
                self.current_date = value
            self._group_date_parts = None
        elif tag == "td" and self._cell is not None:
            self._cell["text"] = " ".join("".join(self._cell["text"]).split())
            if self._date_row:
                self._date_row_parts.append(str(self._cell["text"]))
            elif self._row is not None:
                self._row["cells"].append(self._cell)
            self._cell = None
        elif tag == "tr":
            if self._date_row:
                value = " ".join(" ".join(self._date_row_parts).split())
                if value:
                    self.current_date = value
            elif self._row is not None:
                self._row["date"] = self.current_date
                self.rows.append(self._row)
            self._row = None
            self._date_row = False
            self._date_row_parts = []


def _parse_page_date(value: str | None) -> tuple[int, int, int] | None:
    match = _DATE_RE.fullmatch((value or "").strip())
    if not match:
        return None
    weekday, day, month, year = match.groups()
    try:
        parsed = datetime(int(year), _MONTHS_LONG[month], int(day))
    except (KeyError, ValueError):
        return None
    if parsed.strftime("%A") != weekday:
        return None
    return parsed.year, parsed.month, parsed.day


def _kickoff(value: str | None, clock: str | None) -> datetime | None:
    date_parts = _parse_page_date(value)
    time_match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (clock or "").strip())
    if date_parts is None or time_match is None:
        return None
    local = datetime(
        *date_parts,
        int(time_match.group(1)),
        int(time_match.group(2)),
        tzinfo=_UKT_TZ,
    )
    return local.astimezone(timezone.utc)


def _event_teams(value: str) -> tuple[str, str] | None:
    for separator in (" – ", " — ", " - "):
        if separator in value:
            home, away = value.split(separator, 1)
            if home.strip() and away.strip():
                return home.strip(), away.strip()
    return None


def _field_cells(cells: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(cell.get("field")): cell
        for cell in cells
        if isinstance(cell, Mapping) and cell.get("field")
    }


def _market(
    fields: Mapping[str, Mapping[str, Any]],
    *,
    first_key: str,
    second_key: str,
    line_key: str,
    labels: tuple[str, str],
) -> dict[str, Any] | None:
    first_cell = fields.get(first_key)
    second_cell = fields.get(second_key)
    line_cell = fields.get(line_key)
    if not first_cell or not second_cell or not line_cell:
        return None
    first = _number(first_cell.get("text"), minimum=1.0)
    second = _number(second_cell.get("text"), minimum=1.0)
    line = _number(line_cell.get("text"))
    if first is None or second is None or line is None or line <= -99:
        return None
    probability = _devig_pair(first, second, labels)
    if probability is None:
        return None
    return {
        "line": line,
        f"{labels[0]}_odds": first,
        f"{labels[1]}_odds": second,
        f"{labels[0]}_odds_id": str(first_cell.get("odds_id") or "") or None,
        f"{labels[1]}_odds_id": str(second_cell.get("odds_id") or "") or None,
        "devig_probability": probability,
    }


def parse_oddstorm_asian_page(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = ODDSTORM_CSL_ASIAN_URL,
) -> list[dict[str, Any]]:
    """Parse every complete Asian handicap/total line from a page snapshot."""

    _validate_page_url(url)
    observed = _utc(retrieved_at, field="retrieved_at")
    if len(payload) > MAX_PAGE_BYTES:
        raise ValueError("OddStorm Asian-odds response exceeded 5 MiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("OddStorm Asian-odds response is not UTF-8") from exc
    parser = _AsianPageParser()
    parser.feed(text)
    parser.close()
    source = _source(url=url, payload=payload, retrieved_at=observed)
    result: list[dict[str, Any]] = []
    kickoff_by_match: dict[str, datetime] = {}
    for raw in parser.rows:
        cells = raw["cells"]
        event_cell = next(
            (cell for cell in cells if "od-c-event" in cell.get("class", set())),
            None,
        )
        time_cell = next(
            (cell for cell in cells if "od-c-time" in cell.get("class", set())),
            None,
        )
        if event_cell is None:
            continue
        teams = _event_teams(str(event_cell.get("text") or ""))
        if teams is None:
            continue
        match_id = str(raw["match_id"])
        kickoff = _kickoff(raw.get("date"), str(time_cell.get("text") or "") if time_cell else None)
        if kickoff is not None:
            kickoff_by_match[match_id] = kickoff
        else:
            kickoff = kickoff_by_match.get(match_id)
        if kickoff is None:
            continue
        fields = _field_cells(cells)
        handicap = _market(
            fields,
            first_key="h",
            second_key="a",
            line_key="hhc",
            labels=("home", "away"),
        )
        total = _market(
            fields,
            first_key="o",
            second_key="u",
            line_key="ohc",
            labels=("over", "under"),
        )
        if handicap is None and total is None:
            continue
        pre_match = observed < kickoff
        # The public league page exposes the current line but does not expose
        # a timestamp for that line.  ``observed_at`` proves only that the
        # system saw it before kickoff; it cannot prove when the bookmaker
        # made the line available.  Keep the row for comparison/audit, but do
        # not allow it to become a causal model feature.
        model_eligible = False
        exclusion_reason = (
            "line_timestamp_unavailable"
            if pre_match
            else "first_observation_not_pre_match"
        )
        match_href = str(event_cell.get("href") or "")
        match_url = urllib.parse.urljoin("https://www.oddstorm.com", match_href) if match_href else None
        result.append(
            {
                "match_id": match_id,
                "league_id": ODDSTORM_CSL_LEAGUE_ID,
                "home_team": teams[0],
                "away_team": teams[1],
                "kickoff_at": kickoff.isoformat(),
                "kickoff_time_quality": "exact_provider_display",
                "kickoff_time_basis": "provider_UKT_UTC",
                "match_url": match_url,
                "handicap": handicap,
                "total": total,
                "effective_at": observed.isoformat(),
                "effective_at_source": "observed_at_fallback_no_line_timestamp",
                "observed_at": observed.isoformat(),
                "available_at": observed.isoformat(),
                "enters_model": model_eligible,
                "model_exclusion_reason": exclusion_reason,
                "source": source,
            }
        )
    return result


def _parse_history_effective_at(
    raw_value: str,
    *,
    fixture_kickoff_at: datetime,
    observed_at: datetime,
) -> tuple[datetime | None, str]:
    text = raw_value.strip()
    relative = _RELATIVE_HISTORY_RE.fullmatch(text)
    if relative is not None and any(value is not None for value in relative.groups()):
        days, hours, minutes, seconds = (int(value or 0) for value in relative.groups())
        delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if delta <= timedelta(days=370):
            return observed_at - delta, "provider_relative_age_from_observed_at"
        return None, "unresolved"
    match = _HISTORY_DATE_RE.fullmatch(text)
    if match is None:
        return None, "unresolved"
    weekday, day, month, hour, minute = match.groups()
    fixture = _utc(fixture_kickoff_at, field="fixture_kickoff_at")
    candidates: list[datetime] = []
    for year in (fixture.year - 1, fixture.year, fixture.year + 1):
        try:
            local = datetime(
                year,
                _MONTHS_SHORT[month],
                int(day),
                int(hour),
                int(minute),
                tzinfo=_UKT_TZ,
            )
        except (KeyError, ValueError):
            continue
        if local.strftime("%a") != weekday:
            continue
        effective = local.astimezone(timezone.utc)
        if fixture - timedelta(days=370) <= effective <= fixture + timedelta(days=1):
            candidates.append(effective)
    if len(candidates) != 1:
        return None, "unresolved"
    effective = candidates[0]
    if effective > observed_at:
        return None, "unresolved"
    return effective, "provider_UKT_UTC_year_inferred_from_fixture"


def parse_oddstorm_history(
    payload: bytes,
    *,
    odds_id: str | int,
    fixture_kickoff_at: datetime,
    retrieved_at: datetime,
    url: str | None = None,
) -> list[dict[str, Any]]:
    """Parse one odds ID's history without retroactively exposing it."""

    odds_text = str(odds_id).strip()
    if not odds_text.isdigit():
        raise ValueError("OddStorm odds_id must be numeric")
    history_url = url or f"{ODDSTORM_HISTORY_URL}?id={odds_text}"
    _validate_history_url(history_url, odds_id=odds_text)
    observed = _utc(retrieved_at, field="retrieved_at")
    kickoff = _utc(fixture_kickoff_at, field="fixture_kickoff_at")
    if len(payload) > MAX_HISTORY_BYTES:
        raise ValueError("OddStorm history response exceeded 1 MiB")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("OddStorm history XML containing DTD or entity declarations is rejected")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError("OddStorm history response is not valid XML") from exc
    source = _source(url=history_url, payload=payload, retrieved_at=observed)
    result: list[dict[str, Any]] = []
    for element in root.findall(".//element"):
        raw_odds = (element.findtext("odd") or "").strip()
        raw_date = (element.findtext("date") or "").strip()
        odds = _number(raw_odds, minimum=1.0)
        if odds is None or odds <= 1:
            continue
        effective, effective_source = _parse_history_effective_at(
            raw_date,
            fixture_kickoff_at=kickoff,
            observed_at=observed,
        )
        if effective is None:
            eligible = False
            exclusion_reason = "provider_timestamp_year_not_unambiguous"
            available_at = observed
        else:
            available_at = max(effective, observed)
            eligible = effective < kickoff and available_at < kickoff
            if effective >= kickoff:
                exclusion_reason = "provider_timestamp_not_pre_match"
            elif available_at >= kickoff:
                exclusion_reason = "first_observation_not_pre_match"
            else:
                exclusion_reason = None
        result.append(
            {
                "odds_id": odds_text,
                "decimal_odds": odds,
                "provider_date_raw": raw_date,
                "effective_at": effective.isoformat() if effective is not None else None,
                "effective_at_source": (
                    effective_source
                ),
                "observed_at": observed.isoformat(),
                "available_at": available_at.isoformat(),
                "enters_model": eligible,
                "model_exclusion_reason": exclusion_reason,
                "source": source,
            }
        )
    return result


def latest_paired_history_at_cutoff(
    first_history: Iterable[Mapping[str, Any]],
    second_history: Iterable[Mapping[str, Any]],
    *,
    cutoff_at: datetime,
    line: float,
    market: str,
) -> dict[str, Any] | None:
    """As-of join two independently timestamped sides of a two-way market."""

    cutoff = _utc(cutoff_at, field="cutoff_at")

    def latest(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        candidates: list[tuple[datetime, datetime, Mapping[str, Any]]] = []
        for row in rows:
            if not row.get("enters_model") or not row.get("effective_at") or not row.get("available_at"):
                continue
            try:
                effective = _utc(datetime.fromisoformat(str(row["effective_at"])), field="effective_at")
                available = _utc(datetime.fromisoformat(str(row["available_at"])), field="available_at")
            except (TypeError, ValueError):
                continue
            if effective <= cutoff and available <= cutoff:
                candidates.append((effective, available, row))
        return max(candidates, key=lambda value: (value[0], value[1]))[2] if candidates else None

    first = latest(first_history)
    second = latest(second_history)
    if first is None or second is None:
        return None
    first_odds = _number(first.get("decimal_odds"), minimum=1.0)
    second_odds = _number(second.get("decimal_odds"), minimum=1.0)
    labels = ("home", "away") if market == "asian_handicap" else ("over", "under")
    probability = _devig_pair(first_odds, second_odds, labels)
    if probability is None or first_odds is None or second_odds is None:
        return None
    available = max(
        datetime.fromisoformat(str(first["available_at"])),
        datetime.fromisoformat(str(second["available_at"])),
    ).astimezone(timezone.utc)
    effective = max(
        datetime.fromisoformat(str(first["effective_at"])),
        datetime.fromisoformat(str(second["effective_at"])),
    ).astimezone(timezone.utc)
    return {
        "market": market,
        "line": float(line),
        f"{labels[0]}_odds": first_odds,
        f"{labels[1]}_odds": second_odds,
        f"{labels[0]}_odds_id": str(first.get("odds_id")),
        f"{labels[1]}_odds_id": str(second.get("odds_id")),
        "devig_probability": probability,
        "effective_at": effective.isoformat(),
        "available_at": available.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "enters_model": True,
    }




def fetch_oddstorm_asian_lines(
    *,
    now: datetime | None = None,
    opener: Any | None = None,
    authorization_reference: str | None = None,
) -> dict[str, Any]:
    """Return the v260 block; authorization references are recorded but ignored."""

    observed = _reference_time(now)
    return rights_blocked_envelope(
        SourceId.ODDSTORM_MARKET_COMPARISON,
        provider=SOURCE_NAME,
        checked_at=observed.isoformat(),
        empty_fields=("lines",),
        authorization_reference=authorization_reference,
    )


def fetch_oddstorm_history(
    odds_id: str | int,
    *,
    fixture_kickoff_at: datetime,
    now: datetime | None = None,
    opener: Any | None = None,
    authorization_reference: str | None = None,
) -> dict[str, Any]:
    """Return the v260 block; authorization references are recorded but ignored."""

    observed = _reference_time(now)
    return rights_blocked_envelope(
        SourceId.ODDSTORM_MARKET_HISTORY,
        provider=SOURCE_NAME,
        checked_at=observed.isoformat(),
        empty_fields=("history",),
        authorization_reference=authorization_reference,
    )


__all__ = [
    "ODDSTORM_CSL_ASIAN_URL",
    "ODDSTORM_HISTORY_URL",
    "ODDSTORM_TERMS_URL",
    "fetch_oddstorm_asian_lines",
    "fetch_oddstorm_history",
    "latest_paired_history_at_cutoff",
    "parse_oddstorm_asian_page",
    "parse_oddstorm_history",
]
