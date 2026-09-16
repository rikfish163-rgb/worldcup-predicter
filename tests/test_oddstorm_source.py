from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.oddstorm import (
    ODDSTORM_CSL_ASIAN_URL,
    ODDSTORM_HISTORY_URL,
    fetch_oddstorm_asian_lines,
    fetch_oddstorm_history,
    latest_paired_history_at_cutoff,
    parse_oddstorm_asian_page,
    parse_oddstorm_history,
)
OBSERVED = datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)


def _page() -> bytes:
    return """
    <section id="od-listing" data-lid="2182861">
      <span class="od-group-date">Friday 14 August 2026 UKT</span>
      <table><tbody>
        <tr data-mid="13410978">
          <td class="od-c-time">11:35</td>
          <td class="od-c-event"><a href="/odds/match/13410978-a-vs-b">Shandong Taishan – Qingdao Hainiu</a></td>
          <td data-f="h" data-oid="120982671">1.23</td>
          <td data-f="hhc">0</td>
          <td data-f="a" data-oid="120982673">4.20</td>
          <td data-f="o" data-oid="113991353">1.12</td>
          <td data-f="ohc">1.5</td>
          <td data-f="u" data-oid="113991355">6.13</td>
        </tr>
        <tr data-mid="13410978">
          <td class="od-c-time"></td>
          <td class="od-c-event"><a href="/odds/match/13410978-a-vs-b">Shandong Taishan – Qingdao Hainiu</a></td>
          <td data-f="h" data-oid="113979855">1.81</td>
          <td data-f="hhc">-1</td>
          <td data-f="a" data-oid="113994439">2.05</td>
          <td data-f="o" data-oid="113979843">1.39</td>
          <td data-f="ohc">2.5</td>
          <td data-f="u" data-oid="113979791">2.96</td>
        </tr>
        <tr class="od-daterow"><td colspan="8">Saturday 15 August 2026</td></tr>
        <tr data-mid="13410979">
          <td class="od-c-time">12:00</td>
          <td class="od-c-event"><a href="/odds/match/13410979-c-vs-d">Shanghai Port – Zhejiang</a></td>
          <td data-f="h" data-oid="2101">1.90</td>
          <td data-f="hhc">-0.5</td>
          <td data-f="a" data-oid="2102">1.95</td>
          <td data-f="o" data-oid="2201">2.01</td>
          <td data-f="ohc">3</td>
          <td data-f="u" data-oid="2202">1.85</td>
        </tr>
      </tbody></table>
    </section>
    """.encode()


def test_asian_page_parser_preserves_all_lines_ids_dates_and_raw_hash():
    payload = _page()
    rows = parse_oddstorm_asian_page(payload, retrieved_at=OBSERVED)

    assert len(rows) == 3
    assert rows[0]["match_id"] == "13410978"
    assert rows[0]["home_team"] == "Shandong Taishan"
    assert rows[0]["away_team"] == "Qingdao Hainiu"
    # OddStorm's canonical UKT clock is a fixed UTC base with selectable
    # offsets, not the Europe/London civil-time zone.
    assert rows[0]["kickoff_at"] == "2026-08-14T11:35:00+00:00"
    assert rows[1]["kickoff_at"] == rows[0]["kickoff_at"]
    assert rows[2]["kickoff_at"] == "2026-08-15T12:00:00+00:00"
    assert rows[0]["kickoff_time_basis"] == "provider_UKT_UTC"
    assert rows[1]["handicap"] == {
        "line": -1.0,
        "home_odds": 1.81,
        "away_odds": 2.05,
        "home_odds_id": "113979855",
        "away_odds_id": "113994439",
        "devig_probability": pytest.approx({"home": 0.53108808, "away": 0.46891192}),
    }
    assert rows[1]["total"]["line"] == 2.5
    assert sum(rows[1]["total"]["devig_probability"].values()) == pytest.approx(1.0)
    assert rows[0]["observed_at"] == OBSERVED.isoformat()
    assert rows[0]["effective_at_source"] == "observed_at_fallback_no_line_timestamp"
    # The current page has no line-level publication timestamp.  A pre-match
    # observation is therefore audit/display-only and cannot enter a causal
    # model, even though the fixture itself has not kicked off yet.
    assert rows[0]["enters_model"] is False
    assert rows[0]["model_exclusion_reason"] == "line_timestamp_unavailable"
    assert rows[0]["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert rows[0]["source"]["purchase_eligible"] is False


def test_asian_page_snapshot_after_kickoff_is_audit_only():
    rows = parse_oddstorm_asian_page(
        _page(), retrieved_at=datetime(2026, 8, 16, tzinfo=timezone.utc)
    )
    assert all(row["enters_model"] is False for row in rows)
    assert all(row["model_exclusion_reason"] == "first_observation_not_pre_match" for row in rows)


def test_history_parser_infers_year_from_fixture_but_keeps_first_observation():
    payload = b"""<root>
      <element><odd>1.87</odd><date>Wed 12 Aug 23:10</date></element>
      <element><odd>1.81</odd><date>Thu 13 Aug 00:38</date></element>
    </root>"""
    rows = parse_oddstorm_history(
        payload,
        odds_id="113979855",
        fixture_kickoff_at=datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc),
        retrieved_at=OBSERVED,
    )

    assert len(rows) == 2
    assert rows[1]["odds_id"] == "113979855"
    assert rows[1]["decimal_odds"] == 1.81
    assert rows[1]["effective_at"] == "2026-08-13T00:38:00+00:00"
    assert rows[1]["observed_at"] == OBSERVED.isoformat()
    assert rows[1]["available_at"] == OBSERVED.isoformat()
    assert rows[1]["effective_at_source"] == "provider_UKT_UTC_year_inferred_from_fixture"
    assert rows[1]["enters_model"] is True
    assert rows[1]["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_history_parser_supports_provider_relative_age_without_backdating_visibility():
    rows = parse_oddstorm_history(
        b"<root><element><odd>1.84</odd><date>8 mins 32 secs ago</date></element></root>",
        odds_id="113979855",
        fixture_kickoff_at=datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc),
        retrieved_at=OBSERVED,
    )
    assert rows[0]["effective_at"] == "2026-08-13T02:51:28+00:00"
    assert rows[0]["effective_at_source"] == "provider_relative_age_from_observed_at"
    assert rows[0]["observed_at"] == OBSERVED.isoformat()
    assert rows[0]["available_at"] == OBSERVED.isoformat()
    assert rows[0]["enters_model"] is True


def test_history_with_unresolvable_provider_date_is_quarantined():
    rows = parse_oddstorm_history(
        b"<root><element><odd>1.81</odd><date>Mon 13 Aug 00:38</date></element></root>",
        odds_id="113979855",
        fixture_kickoff_at=datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc),
        retrieved_at=OBSERVED,
    )
    assert rows[0]["effective_at"] is None
    assert rows[0]["enters_model"] is False
    assert rows[0]["model_exclusion_reason"] == "provider_timestamp_year_not_unambiguous"


def test_history_pair_requires_both_sides_to_have_been_observed_by_cutoff():
    kickoff = datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc)
    home = parse_oddstorm_history(
        b"<root><element><odd>1.81</odd><date>Thu 13 Aug 00:38</date></element></root>",
        odds_id="113979855",
        fixture_kickoff_at=kickoff,
        retrieved_at=OBSERVED,
    )
    away = parse_oddstorm_history(
        b"<root><element><odd>2.05</odd><date>Thu 13 Aug 00:40</date></element></root>",
        odds_id="113994439",
        fixture_kickoff_at=kickoff,
        retrieved_at=OBSERVED,
    )

    assert latest_paired_history_at_cutoff(
        home,
        away,
        cutoff_at=datetime(2026, 8, 13, 2, 59, tzinfo=timezone.utc),
        line=-1,
        market="asian_handicap",
    ) is None
    pair = latest_paired_history_at_cutoff(
        home,
        away,
        cutoff_at=datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc),
        line=-1,
        market="asian_handicap",
    )
    assert pair is not None
    assert pair["line"] == -1.0
    assert pair["home_odds"] == 1.81
    assert pair["away_odds"] == 2.05
    assert sum(pair["devig_probability"].values()) == pytest.approx(1.0)
    assert pair["available_at"] == OBSERVED.isoformat()


def test_history_parser_rejects_entities_and_non_allowlisted_urls():
    with pytest.raises(ValueError, match="DTD|entity"):
        parse_oddstorm_history(
            b'<!DOCTYPE x [<!ENTITY y "bad">]><root><element><odd>&y;</odd></element></root>',
            odds_id="1",
            fixture_kickoff_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
            retrieved_at=OBSERVED,
        )
    with pytest.raises(ValueError, match="allowlisted"):
        parse_oddstorm_asian_page(
            _page(), retrieved_at=OBSERVED, url="https://example.invalid/asianodds/league/1"
        )
    with pytest.raises(ValueError, match="allowlisted"):
        parse_oddstorm_history(
            b"<root />",
            odds_id="1",
            fixture_kickoff_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
            retrieved_at=OBSERVED,
            url="https://example.invalid/odds/oddhistory?id=1",
        )


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

    def read(self, _limit):
        return self.payload


class _Opener:
    def __init__(self, routes: dict[str, bytes], *, redirect_to: str | None = None):
        self.routes = routes
        self.redirect_to = redirect_to
        self.calls: list[tuple[str, float]] = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        payload = self.routes[request.full_url]
        return _Response(payload, self.redirect_to or request.full_url)


def _assert_oddstorm_block(
    payload: dict,
    *,
    source_id: str,
    empty_field: str,
    ignored_inputs: list[str],
) -> None:
    assert payload["status"] == "rights_blocked"
    assert payload["retrieved_at"] is None
    assert payload["checked_at"] == OBSERVED.isoformat()
    assert payload["rights_status"] == "blocked_pending_express_written_permission"
    assert payload["network_opened"] is False
    assert payload["errors"] == []
    assert payload[empty_field] == []
    assert payload["rights"]["source_id"] == source_id
    assert payload["rights"]["terms_url"] == "https://www.oddstorm.com/terms"
    assert payload["rights"]["ignored_inputs"] == ignored_inputs


def test_oddstorm_fetchers_fail_closed_before_network_without_written_permission():
    class ExplodingOpener:
        def __init__(self):
            self.calls = 0

        def open(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("network must not open before written authorization")

    page_opener = ExplodingOpener()
    history_opener = ExplodingOpener()
    page = fetch_oddstorm_asian_lines(now=OBSERVED, opener=page_opener)
    history = fetch_oddstorm_history(
        "113979855",
        fixture_kickoff_at=datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc),
        now=OBSERVED,
        opener=history_opener,
    )

    _assert_oddstorm_block(
        page,
        source_id="oddstorm_market_comparison",
        empty_field="lines",
        ignored_inputs=[],
    )
    _assert_oddstorm_block(
        history,
        source_id="oddstorm_market_history",
        empty_field="history",
        ignored_inputs=[],
    )
    assert page_opener.calls == history_opener.calls == 0


def test_self_issued_authorization_cannot_uplift_oddstorm_fetchers():
    history_url = f"{ODDSTORM_HISTORY_URL}?id=113979855"
    opener = _Opener({ODDSTORM_CSL_ASIAN_URL: _page(), history_url: b"<root />"})
    page = fetch_oddstorm_asian_lines(
        now=OBSERVED,
        opener=opener,
        authorization_reference="test-written-permission",
    )
    history = fetch_oddstorm_history(
        "113979855",
        fixture_kickoff_at=datetime(2026, 8, 14, 11, 35, tzinfo=timezone.utc),
        now=OBSERVED,
        opener=opener,
        authorization_reference="test-written-permission",
    )
    _assert_oddstorm_block(
        page,
        source_id="oddstorm_market_comparison",
        empty_field="lines",
        ignored_inputs=["authorization_reference"],
    )
    _assert_oddstorm_block(
        history,
        source_id="oddstorm_market_history",
        empty_field="history",
        ignored_inputs=["authorization_reference"],
    )
    assert opener.calls == []


def test_oddstorm_rights_gate_precedes_empty_page_retry():
    class SequenceOpener:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.calls = []

        def open(self, request, timeout):
            self.calls.append((request.full_url, request.headers.get("Cache-control")))
            payload = self.payloads.pop(0)
            return _Response(payload, request.full_url)

    opener = SequenceOpener([b"<html></html>", _page()])
    result = fetch_oddstorm_asian_lines(
        now=OBSERVED,
        opener=opener,
        authorization_reference="test-written-permission",
    )

    _assert_oddstorm_block(
        result,
        source_id="oddstorm_market_comparison",
        empty_field="lines",
        ignored_inputs=["authorization_reference"],
    )
    assert opener.calls == []


def test_oddstorm_rights_gate_precedes_persistent_empty_page_handling():
    class EmptyOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            return _Response(b"<html></html>", request.full_url)

    opener = EmptyOpener()
    result = fetch_oddstorm_asian_lines(
        now=OBSERVED,
        opener=opener,
        authorization_reference="test-written-permission",
    )

    _assert_oddstorm_block(
        result,
        source_id="oddstorm_market_comparison",
        empty_field="lines",
        ignored_inputs=["authorization_reference"],
    )
    assert opener.calls == 0


def test_oddstorm_rights_gate_precedes_redirect_validation():
    redirected = _Opener(
        {ODDSTORM_CSL_ASIAN_URL: _page()}, redirect_to="https://example.invalid/stolen"
    )
    result = fetch_oddstorm_asian_lines(
        now=OBSERVED,
        opener=redirected,
        authorization_reference="test-written-permission",
    )
    _assert_oddstorm_block(
        result,
        source_id="oddstorm_market_comparison",
        empty_field="lines",
        ignored_inputs=["authorization_reference"],
    )
    assert redirected.calls == []
