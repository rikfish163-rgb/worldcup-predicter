"""Read-only CheckBestOdds historical market evidence adapter.

The site exposes Asian-handicap sections through the same public XAJAX call
used by its match page.  This adapter keeps those sections as evidence only:
the response contains the last displayed odds, but does not expose a trusted
publication timestamp for each line, so callers must not use it as a frozen
pre-match feature without an independent effective-time check.
"""

from __future__ import annotations

import hashlib
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


CHECKBESTODDS_HOST = "checkbestodds.com"
CHECKBESTODDS_SOURCE_NAME = "CheckBestOdds"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_CONTENT_BYTES = 10 * 1024 * 1024
_CHINA_MATCH_PATH = re.compile(r"^/football-odds/china/[^/]+/\d+$")


def _utc_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CHECKBESTODDS_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or _CHINA_MATCH_PATH.fullmatch(parsed.path) is None
    ):
        raise ValueError("CheckBestOdds URL is not allowlisted")


def _hash_source(*, url: str, retrieved_at: datetime, payload: bytes) -> dict:
    return {
        "name": CHECKBESTODDS_SOURCE_NAME,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_checkbestodds_match_page(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse match identity and the XAJAX arguments embedded in a public page."""

    _validate_url(url)
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    soup = BeautifulSoup(payload, "html.parser")
    match_id = soup.find("input", id="id_match_chart")
    match_hash = soup.find("input", id="matchHash")
    time_node = soup.select_one("#matchTime[ts], span.YmdHM[ts]")
    teams_node = soup.select_one("span.tblehead span.o, div.tblehead span.o")
    if not match_id or not match_id.get("value"):
        raise ValueError("CheckBestOdds page is missing match ID")
    if not match_hash or not match_hash.get("value"):
        raise ValueError("CheckBestOdds page is missing match hash")
    if not time_node or not time_node.get("ts"):
        raise ValueError("CheckBestOdds page is missing kickoff timestamp")
    if not teams_node:
        raise ValueError("CheckBestOdds page is missing team names")
    try:
        kickoff_at = datetime.fromtimestamp(float(time_node["ts"]), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("CheckBestOdds page has invalid kickoff timestamp") from exc
    team_names = [
        part.strip()
        for part in re.split(r"\s+-\s+", teams_node.get_text(" ", strip=True), maxsplit=1)
    ]
    if len(team_names) != 2 or not all(team_names):
        raise ValueError("CheckBestOdds page has invalid home/away teams")
    return {
        "match_id": str(match_id["value"]),
        "match_hash": str(match_hash["value"]),
        "match_time": str(time_node["ts"]),
        "kickoff_at": kickoff_at.isoformat(),
        "home_team": team_names[0],
        "away_team": team_names[1],
        "source": _hash_source(url=url, retrieved_at=observed, payload=payload),
    }


def _number(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 1.0 else None


def _section_rows(section) -> tuple[list[dict], int]:
    table = section.find("table")
    if table is None:
        return [], 0
    rows = []
    for row in table.find_all("tr"):
        if row.find("th") or "maxOdds" in (row.get("class") or []):
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        anchor = cells[0].find("a", class_="toSort")
        bookmaker = (
            anchor.get_text(" ", strip=True) if anchor else cells[0].get_text(" ", strip=True)
        )
        home = _number(cells[1].get_text(" ", strip=True))
        away = _number(cells[2].get_text(" ", strip=True))
        if bookmaker and home is not None and away is not None:
            rows.append({"bookmaker": bookmaker, "home_odds": home, "away_odds": away})
    footer = table.find("tr", class_="maxOdds")
    if footer is None:
        return rows, len(rows)
    cells = footer.find_all("td", recursive=False)
    if len(cells) < 3:
        return rows, len(rows)
    best_home = _number(cells[1].get_text(" ", strip=True))
    best_away = _number(cells[2].get_text(" ", strip=True))
    if best_home is not None and best_away is not None:
        rows.append({"bookmaker": "Best odds", "home_odds": best_home, "away_odds": best_away})
    return rows, len([row for row in rows if row["bookmaker"] != "Best odds"])


def parse_checkbestodds_more_odds(
    payload: bytes,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    url: str,
) -> dict:
    """Parse a public ``moreOddsFootball`` XML response.

    ``effective_at`` intentionally remains ``None``.  CheckBestOdds labels the
    table as the last odds but does not expose a trusted per-line timestamp.
    """

    _validate_url(url)
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    try:
        xml = BeautifulSoup(payload, "xml")
        command = xml.find("cmd")
        content = command.string if command is not None else None
    except Exception as exc:  # pragma: no cover - parser library failure guard
        raise ValueError("CheckBestOdds XAJAX response is not parseable XML") from exc
    if not content:
        raise ValueError("CheckBestOdds XAJAX response is missing CDATA")
    soup = BeautifulSoup(str(content), "html.parser")
    markets = []
    for header in soup.find_all("div", class_="tblehead"):
        title = header.get_text(" ", strip=True)
        if "Asian Handicap" not in title:
            continue
        period = "first_half" if "1st Half" in title else "full_time"
        section = header.find_next_sibling("div", class_="tblediv")
        if section is None:
            continue
        home_line = section.find("input", class_="homeLine")
        away_line = section.find("input", class_="awayLine")
        if (
            not home_line
            or not away_line
            or not home_line.get("value")
            or not away_line.get("value")
        ):
            continue
        rows, bookmaker_count = _section_rows(section)
        if not rows:
            continue
        markets.append(
            {
                "market": "asian_handicap",
                "period": period,
                "home_line": str(home_line["value"]),
                "away_line": str(away_line["value"]),
                "bookmaker_count": bookmaker_count,
                "odds": rows,
                "effective_at": None,
                "effective_time_status": "unknown_last_displayed_odds",
                "model_eligible": False,
            }
        )
    if not markets:
        raise ValueError("CheckBestOdds XAJAX response has no Asian Handicap section")
    return {
        "fixture_id": fixture_id,
        "markets": markets,
        "source": _hash_source(url=url, retrieved_at=observed, payload=payload),
    }


def _read_limited(opener, request: urllib.request.Request) -> bytes:
    response = opener.open(request, timeout=DEFAULT_TIMEOUT_SECONDS)
    with response:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        _validate_url(final_url)
        payload = response.read(MAX_CONTENT_BYTES + 1)
    if not isinstance(payload, bytes) or len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("CheckBestOdds response exceeded 10 MiB")
    return payload


def fetch_checkbestodds_match(
    url: str,
    *,
    fixture_id: str,
    retrieved_at: datetime,
    opener=None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch one historical match page and its page-owned public XAJAX data."""

    return rights_blocked_fetch_envelope(
        SourceId.CHECKBESTODDS_PUBLIC_PAGES,
        provider=CHECKBESTODDS_SOURCE_NAME,
        now=retrieved_at,
        empty_fields=("matches", "markets"),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    _validate_url(url)
    observed = _utc_datetime(retrieved_at, field="retrieved_at")
    if opener is None:

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                raise urllib.error.URLError("redirects are disabled for source fetches")

        opener = urllib.request.build_opener(_NoRedirect())
    page_request = urllib.request.Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "Matchline/1.0 (+public-source)"},
    )
    page_payload = _read_limited(opener, page_request)
    match = parse_checkbestodds_match_page(page_payload, retrieved_at=observed, url=url)
    args = [
        "0,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,24,25,26,27,28,29,30,31,32,33,34,35,36",
        match["match_time"],
        match["home_team"],
        match["away_team"],
        match["match_hash"],
    ]
    body = urllib.parse.urlencode(
        [("xjxcls", "fx"), ("xjxmthd", "moreOddsFootball"), ("xjxr", "0")]
        + [("xjxargs[]", f"S{value}") for value in args]
    ).encode()
    ajax_request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    ajax_payload = _read_limited(opener, ajax_request)
    market = parse_checkbestodds_more_odds(
        ajax_payload,
        fixture_id=fixture_id,
        retrieved_at=observed,
        url=url,
    )
    return {
        "match": match,
        "markets": market["markets"],
        "source": market["source"],
        "status": "available",
    }


__all__ = [
    "fetch_checkbestodds_match",
    "parse_checkbestodds_match_page",
    "parse_checkbestodds_more_odds",
]
