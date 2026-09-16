from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from league_platform.sources.checkbestodds import (
    fetch_checkbestodds_match,
    parse_checkbestodds_match_page,
    parse_checkbestodds_more_odds,
)


AS_OF = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
URL = "https://checkbestodds.com/football-odds/china/home-team-away-team-2025-11-01/1544476209"


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, _limit):
        return self.payload


class _Opener:
    def __init__(self, page: bytes, market: bytes):
        self.page = page
        self.market = market
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        payload = self.page if request.data is None else self.market
        return _Response(payload, request.full_url)


def _page() -> bytes:
    return b"""
    <html><body>
      <div id='matchTime' ts='1761982200'>2025-11-01 08:30</div>
      <div class='tblehead'><span class='o'>Home Team - Away Team</span></div>
      <input id='id_match_chart' value='1544476209'>
      <input id='matchHash' value='abc123'>
    </body></html>
    """


def _market() -> bytes:
    return b"""<?xml version='1.0'?><xjx><cmd cmd='as' id='moreOdds' prop='innerHTML'><![CDATA[
      <div class='tblehead'><span>Asian Handicap +0.25; -0.25</span></div>
      <div class='tblediv'>
        <input class='homeLine' value='+0.25'><input class='awayLine' value='-0.25'>
        <table class='tble'><thead><tr><th>Bookmaker</th><th>+0.25</th><th>-0.25</th></tr></thead>
          <tr><td><a class='toSort'>Book A</a></td><td>1.92</td><td>1.98</td></tr>
          <tfoot><tr class='maxOdds'><td>Best odds</td><td>1.95</td><td>2.02</td></tr></tfoot>
        </table>
      </div>
    ]]></cmd></xjx>"""


def test_match_page_parser_preserves_time_and_provenance():
    payload = _page()
    result = parse_checkbestodds_match_page(payload, retrieved_at=AS_OF, url=URL)
    assert result["match_time"] == "1761982200"
    assert result["kickoff_at"] == "2025-11-01T07:30:00+00:00"
    assert result["home_team"] == "Home Team"
    assert result["away_team"] == "Away Team"
    assert result["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_more_odds_parser_keeps_lines_and_blocks_unknown_effective_time():
    payload = _market()
    result = parse_checkbestodds_more_odds(
        payload, fixture_id="csl:1", retrieved_at=AS_OF, url=URL
    )
    market = result["markets"][0]
    assert market["home_line"] == "+0.25"
    assert market["away_line"] == "-0.25"
    assert market["bookmaker_count"] == 1
    assert market["odds"][-1] == {"bookmaker": "Best odds", "home_odds": 1.95, "away_odds": 2.02}
    assert market["effective_at"] is None
    assert market["model_eligible"] is False


def test_fetch_uses_page_owned_public_xajax_call():
    opener = _Opener(_page(), _market())
    result = fetch_checkbestodds_match(URL, fixture_id="csl:1", retrieved_at=AS_OF, opener=opener)
    assert result["status"] == "rights_blocked"
    assert opener.calls == []
    assert result["matches"] == []
    assert result["markets"] == []
    assert result["rights"]["source_id"] == "checkbestodds_public_pages"


def test_checkbestodds_rejects_non_china_or_non_https_urls():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_checkbestodds_match_page(
            _page(), retrieved_at=AS_OF, url="https://example.invalid/match"
        )
