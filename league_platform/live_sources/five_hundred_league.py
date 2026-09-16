"""Public 500.com league-page audit adapter.

The league pages expose useful historical context (fixture identity, final
score, and the page's selected Asian line), but they are season aggregates.
They do not prove when the market line was available to Matchline before
kickoff.  This adapter therefore preserves the rows for an auditable source
ledger and *never* promotes them into a causal model snapshot.

Only the public league-page route is allowlisted.  The site's JavaScript uses
query-string endpoints to load other rounds; those endpoints are not used
here because the provider's robots policy disallows that route.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


FIVE_HUNDRED_HOST = "liansai.500.com"
FIVE_HUNDRED_LEAGUE_URL_TEMPLATE = "https://liansai.500.com/zuqiu-{season_id}/jifen-{stage_id}/"
FIVE_HUNDRED_SOURCE_NAME = "500彩票网 public league page"
MAX_PAGE_BYTES = 5 * 1024 * 1024
_CHINA_TZ = ZoneInfo("Asia/Shanghai")
_PAGE_PATH_RE = re.compile(r"^/zuqiu-(\d+)/jifen-(\d+)/$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any):
        raise urllib.error.URLError("500 league page redirected; refusing to follow it")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def build_league_page_url(season_id: int | str, stage_id: int | str) -> str:
    """Build one static public league-page URL."""

    season = str(season_id).strip()
    stage = str(stage_id).strip()
    if not season.isdigit() or not stage.isdigit() or int(season) < 1 or int(stage) < 1:
        raise ValueError("season_id and stage_id must be positive integers")
    return FIVE_HUNDRED_LEAGUE_URL_TEMPLATE.format(season_id=season, stage_id=stage)


def _validate_page_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != FIVE_HUNDRED_HOST
        or parsed.port not in (None, 443)
        or not _PAGE_PATH_RE.fullmatch(parsed.path)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("500 league page URL is not allowlisted")


def _decode_page(payload: bytes) -> str:
    # The provider declares gbk and the real pages contain simplified Chinese.
    # gb18030 is a strict superset for the relevant code points.
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("500 league page is not valid UTF-8/GBK text")


def _clean(value: str) -> str:
    return " ".join(value.split())


def _score_pair(value: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*:\s*(\d+)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _line_number(value: str) -> float | None:
    """Parse the provider's Chinese Asian-line display without asserting side orientation."""

    text = _clean(value).replace(" ", "")
    if not text:
        return None
    aliases = {
        "平手": 0.0,
        "平手/半球": 0.25,
        "半球/平手": 0.25,
        "半球": 0.5,
        "半球/一球": 0.75,
        "一球/半球": 0.75,
        "一球": 1.0,
        "一球/球半": 1.25,
        "球半/一球": 1.25,
        "球半": 1.5,
        "球半/两球": 1.75,
        "两球/球半": 1.75,
        "两球": 2.0,
        "两球/两球半": 2.25,
        "两球半/两球": 2.25,
        "两球半": 2.5,
        "三球": 3.0,
    }
    if text in aliases:
        return aliases[text]
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


class _LeaguePageParser(HTMLParser):
    """Extract only the stable attributes/cells from the server-rendered table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._cell: dict[str, Any] | None = None
        self._anchor_title: str | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        if tag == "tr" and values.get("data-fid", "").strip():
            self._row = {
                "fixture_id": values["data-fid"].strip(),
                "home_provider_id": values.get("data-hid", "").strip() or None,
                "away_provider_id": values.get("data-gid", "").strip() or None,
                "status": values.get("data-status", "").strip() or None,
                "home_score": values.get("data-hscore", "").strip() or None,
                "away_score": values.get("data-ascore", "").strip() or None,
                "kickoff_text": values.get("data-time", "").strip() or None,
                "home_team": None,
                "away_team": None,
                "score_text": [],
                "hidden_cells": [],
                "current_team": None,
            }
            return
        if self._row is None:
            return
        if tag == "td":
            classes = set(values.get("class", "").split())
            self._cell = {"classes": classes, "text": []}
        elif tag == "a" and self._cell is not None:
            self._anchor_title = values.get("title") or None

    def handle_data(self, data: str) -> None:
        if self._cell is None or self._row is None:
            return
        self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if tag == "a":
            if self._anchor_title:
                if self._row["home_team"] is None:
                    self._row["home_team"] = _clean(self._anchor_title)
                elif self._row["away_team"] is None:
                    self._row["away_team"] = _clean(self._anchor_title)
            self._anchor_title = None
        elif tag == "td" and self._cell is not None:
            text = _clean("".join(self._cell["text"]))
            classes = self._cell["classes"]
            if "td_lteam" in classes and self._row["home_team"] is None and text:
                self._row["home_team"] = text
            elif "td_rteam" in classes and self._row["away_team"] is None and text:
                self._row["away_team"] = text
            if "hidetagforcheck" in classes:
                self._row["hidden_cells"].append(text)
            if "td_time" in classes and text:
                self._row["kickoff_text"] = text
            if "td_lteam" in classes or "td_rteam" in classes:
                self._row["current_team"] = None
            if text and not ("td_lteam" in classes or "td_rteam" in classes):
                self._row["score_text"].append(text)
            self._cell = None
        elif tag == "tr":
            self.rows.append(self._row)
            self._row = None


def parse_league_page(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str,
) -> list[dict[str, Any]]:
    """Parse rows and explicitly mark every page-derived market as audit-only."""

    _validate_page_url(url)
    observed = _utc(retrieved_at)
    if len(payload) > MAX_PAGE_BYTES:
        raise ValueError("500 league page exceeded the configured size limit")
    parser = _LeaguePageParser()
    parser.feed(_decode_page(payload))
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    rows: list[dict[str, Any]] = []
    for raw in parser.rows:
        score: tuple[int, int] | None = None
        if raw["home_score"] and raw["away_score"]:
            try:
                score = int(raw["home_score"]), int(raw["away_score"])
            except ValueError:
                score = None
        if score is None:
            score = _score_pair(" ".join(raw["score_text"]))
        kickoff_at: datetime | None = None
        kickoff_quality = "unavailable"
        if raw["kickoff_text"]:
            try:
                kickoff_at = datetime.strptime(raw["kickoff_text"], "%Y-%m-%d %H:%M").replace(
                    tzinfo=_CHINA_TZ
                )
                kickoff_quality = "exact_provider_page_timestamp"
            except ValueError:
                kickoff_quality = "invalid_provider_page_timestamp"
        hidden = raw["hidden_cells"]
        line_text = hidden[1] if len(hidden) > 1 else ""
        line_result = hidden[2] if len(hidden) > 2 else ""
        rows.append(
            {
                "fixture_id": f"500:{raw['fixture_id']}",
                "provider_fixture_id": raw["fixture_id"],
                "home_team": raw["home_team"] or "",
                "away_team": raw["away_team"] or "",
                "home_provider_id": raw["home_provider_id"],
                "away_provider_id": raw["away_provider_id"],
                "kickoff_at": kickoff_at.astimezone(timezone.utc).isoformat()
                if kickoff_at is not None
                else None,
                "kickoff_time_quality": kickoff_quality,
                "score": {"home": score[0], "away": score[1]} if score else None,
                "score_status": "final"
                if raw["status"] == "5" and score
                else "not_final_or_unparseable",
                "handicap_line_text": line_text or None,
                "handicap_line_magnitude": _line_number(line_text),
                "handicap_result_text": line_result or None,
                "handicap_orientation": "unverified_provider_orientation",
                "market_time_basis": "historical_aggregate_page_no_pre_kickoff_timestamp",
                "effective_at": None,
                "effective_at_source": "unavailable_historical_aggregate_page",
                "observed_at": observed.isoformat(),
                "enters_model": False,
                "model_exclusion_reason": "no_pre_kickoff_market_timestamp",
                "source": {
                    "name": FIVE_HUNDRED_SOURCE_NAME,
                    "url": url,
                    "source_level": "public_historical_page",
                    "raw_sha256": raw_sha256,
                    "retrieved_at": observed.isoformat(),
                    "selected_asian_company": "5",
                    "official_sports_lottery": False,
                    "purchase_eligible": False,
                },
            }
        )
    return rows


def _opener(opener: Any | None) -> Any:
    return opener or urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def fetch_league_page(
    season_id: int | str,
    stage_id: int | str,
    *,
    now: datetime | None = None,
    opener: Any | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Fetch one allowlisted page; failures stay isolated from other sources."""

    return rights_blocked_fetch_envelope(
        SourceId.FIVE_HUNDRED_LEAGUE_PUBLIC_PAGES,
        provider=FIVE_HUNDRED_SOURCE_NAME,
        now=now,
        empty_fields=("rows",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    observed = _utc(now or datetime.now(timezone.utc))
    url = build_league_page_url(season_id, stage_id)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": "Matchline/1.0 (+public-source-audit)",
        },
    )
    try:
        with _opener(opener).open(request, timeout=25) as response:
            final_url = response.geturl()
            _validate_page_url(final_url)
            payload = response.read(MAX_PAGE_BYTES + 1)
        if len(payload) > MAX_PAGE_BYTES:
            raise ValueError("500 league page exceeded the configured size limit")
        rows = parse_league_page(payload, retrieved_at=observed, url=final_url)
        return {
            "provider": FIVE_HUNDRED_SOURCE_NAME,
            "request_url": final_url,
            "retrieved_at": observed.isoformat(),
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
            "rows": rows,
            "errors": [],
            "status": "available" if rows else "empty",
        }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {
            "provider": FIVE_HUNDRED_SOURCE_NAME,
            "request_url": url,
            "retrieved_at": observed.isoformat(),
            "rows": [],
            "errors": [{"stage": "fetch_or_parse", "error": str(exc)}],
            "status": "unavailable",
        }
