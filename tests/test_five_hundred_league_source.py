from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.five_hundred_league import (
    build_league_page_url,
    fetch_league_page,
    parse_league_page,
)


OBSERVED = datetime(2026, 8, 13, 18, 30, tzinfo=timezone.utc)


def _page() -> bytes:
    return """
    <html><body>
      <table><tbody>
        <tr data-fid="1134519" data-hid="672" data-gid="1367" data-status="5"
            data-hscore="1" data-ascore="1" data-time="2024-11-02 15:30">
          <td>30</td>
          <td class="td_time">2024-11-02 15:30</td>
          <td class="td_lteam"><a title="北京国安">北京国安</a></td>
          <td><span>1</span>:<span>1</span> (1:1)</td>
          <td class="td_rteam"><a title="河南队">河南队</a></td>
          <td>平</td>
          <td class="hidetagforcheck"><span>1.53</span><span>4.26</span><span>5.13</span></td>
          <td class="hidetagforcheck">一球</td>
          <td class="hidetagforcheck"><span>输</span></td>
          <td class="hidetagforcheck"><span>小</span></td>
        </tr>
      </tbody></table>
    </body></html>
    """.encode("gb18030")


def test_page_parser_preserves_fixture_and_marks_market_audit_only():
    payload = _page()
    rows = parse_league_page(
        payload,
        retrieved_at=OBSERVED,
        url="https://liansai.500.com/zuqiu-7119/jifen-20602/",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["fixture_id"] == "500:1134519"
    assert row["home_team"] == "北京国安"
    assert row["away_team"] == "河南队"
    assert row["kickoff_at"] == "2024-11-02T07:30:00+00:00"
    assert row["score"] == {"home": 1, "away": 1}
    assert row["handicap_line_text"] == "一球"
    assert row["handicap_line_magnitude"] == 1.0
    assert row["handicap_result_text"] == "输"
    assert row["effective_at"] is None
    assert row["market_time_basis"] == "historical_aggregate_page_no_pre_kickoff_timestamp"
    assert row["enters_model"] is False
    assert row["model_exclusion_reason"] == "no_pre_kickoff_market_timestamp"
    assert row["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_page_parser_accepts_static_route_only_and_rejects_query_endpoint():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_league_page(
            _page(),
            retrieved_at=OBSERVED,
            url="https://liansai.500.com/index.php?c=score&a=getmatch&stid=20602&round=1",
        )


def test_url_builder_rejects_non_numeric_ids():
    assert build_league_page_url(7119, 20602).endswith("/zuqiu-7119/jifen-20602/")
    with pytest.raises(ValueError):
        build_league_page_url("7119?x=1", 20602)


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _limit: int):
        return self.payload


class _Opener:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def open(self, _request, timeout: int):
        assert timeout == 25
        return _Response(self.payload, self.url)


def test_fetch_keeps_source_failure_isolated_and_accepts_valid_static_page():
    result = fetch_league_page(
        7119,
        20602,
        now=OBSERVED,
        opener=_Opener(_page(), "https://liansai.500.com/zuqiu-7119/jifen-20602/"),
    )
    assert result["status"] == "rights_blocked"
    assert result["rows"] == []
    assert result["errors"] == []

    failed = fetch_league_page(
        7119,
        20602,
        now=OBSERVED,
        opener=_Opener(_page(), "https://liansai.500.com/zuqiu-7119/jifen-20602/?round=1"),
    )
    assert failed["status"] == "rights_blocked"
    assert failed["rows"] == []
    assert failed["errors"] == []
    assert failed["rights"]["source_id"] == "five_hundred_league_public_pages"
