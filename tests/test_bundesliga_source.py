from __future__ import annotations

from datetime import datetime, timezone

from league_platform.live_sources.bundesliga import (
    discover_match_urls,
    fetch_bundesliga_lineups,
    parse_bundesliga_match_page,
)


MATCHDAY_URL = "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1"
MATCH_URL = (
    "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/"
    "fc-bayern-muenchen-vs-vfb-stuttgart/liveticker"
)
LINEUP_URL = MATCH_URL.replace("/liveticker", "/lineup")
KOELN_LINEUP_URL = (
    "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/"
    "fc-bayern-muenchen-vs-1-fc-koeln/lineup"
)


def _lineup_html(*, complete: bool) -> bytes:
    names = [f"Player {index}" for index in range(11 if complete else 0)]
    columns = []
    for side in ("home", "away"):
        players = "".join(
            f'<div class="player-name"><span class="name lastName">{side} {name}</span></div>'
            for name in names
        )
        columns.append(f'<div class="col col-12 col-md-6">{players}</div>')
    return (
        '<meta name="keywords" content="Bundesliga,2026-08-28T18:30:00+0000">'
        '<div class="tactical-lineup">' + "".join(columns) + "</div>"
    ).encode()


class _Response:
    def __init__(self, url: str, payload: bytes):
        self._url = url
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, _limit: int):
        return self._payload


class _Opener:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def open(self, request, timeout):
        del timeout
        return _Response(request.full_url, self.payloads[request.full_url])


def test_discover_match_urls_allowlists_public_match_routes():
    payload = f'<a href="{MATCH_URL}">Bayern - Stuttgart</a>'.encode()
    assert discover_match_urls(payload, url=MATCHDAY_URL) == [MATCH_URL]


def test_parse_complete_pre_kickoff_xi_is_model_eligible():
    row = parse_bundesliga_match_page(
        _lineup_html(complete=True),
        fixture_id="espn:1",
        kickoff_at="2026-08-28T18:30:00+00:00",
        retrieved_at=datetime(2026, 8, 28, 16, tzinfo=timezone.utc),
        url=LINEUP_URL,
        expected_home="Bayern Munich",
        expected_away="VfB Stuttgart",
    )
    assert row["lineups"]["confirmed"] is True
    assert row["lineups"]["model_eligible"] is True
    assert len(row["lineups"]["home"]["players"]) == 11
    assert len(row["lineups"]["away"]["players"]) == 11


def test_parse_accepts_exact_german_umlaut_slug_transliteration():
    row = parse_bundesliga_match_page(
        _lineup_html(complete=True),
        fixture_id="espn:koeln",
        kickoff_at="2026-08-28T18:30:00+00:00",
        retrieved_at=datetime(2026, 8, 28, 16, tzinfo=timezone.utc),
        url=KOELN_LINEUP_URL,
        expected_home="FC Bayern München",
        expected_away="1. FC Köln",
    )

    assert row["lineups"]["confirmed"] is True


def test_incomplete_or_post_kickoff_xi_remains_display_only():
    row = parse_bundesliga_match_page(
        _lineup_html(complete=False),
        fixture_id="espn:1",
        kickoff_at="2026-08-28T18:30:00+00:00",
        retrieved_at=datetime(2026, 8, 28, 19, tzinfo=timezone.utc),
        url=LINEUP_URL,
        expected_home="Bayern Munich",
        expected_away="VfB Stuttgart",
    )
    assert row["lineups"]["confirmed"] is False
    assert row["lineups"]["model_eligible"] is False


def test_empty_official_transfer_state_is_not_published_not_a_source_failure():
    payload = (
        '<meta name="keywords" content="Bundesliga,2026-08-28T18:30:00+0000">'
        '<script id="ng-state" type="application/json">'
        '{"_getDataFromFirebase-\\u002Fall\\u002FDFL-COM-000001\\u002Fseasons'
        "\\u002FDFL-SEA-0001KA\\u002Fmatchdays\\u002FDFL-DAY-004CBT"
        '\\u002FDFL-MAT-J043GB\\u002Flineup":'
        '{"lastUpdateDateTime":"2026-07-02T08:58:45.915Z"}}'
        "</script>"
    ).encode()

    row = parse_bundesliga_match_page(
        payload,
        fixture_id="espn:1",
        kickoff_at="2026-08-28T18:30:00+00:00",
        retrieved_at=datetime(2026, 8, 23, 7, 15, tzinfo=timezone.utc),
        url=LINEUP_URL,
        expected_home="Bayern Munich",
        expected_away="VfB Stuttgart",
    )

    assert row["lineups"]["available"] is False
    assert row["lineups"]["diagnostic"] == {
        "status": "not_published",
        "reason": "official_feed_returned_no_lineup_rows",
        "home_player_count": 0,
        "away_player_count": 0,
        "home_starter_count": 0,
        "away_starter_count": 0,
        "provider_last_updated_at": "2026-07-02T08:58:45.915Z",
    }
    assert row["source"]["source_kind"] == "official_lineup_status"


def test_fetch_is_bounded_and_strictly_joins_fixture():
    opener = _Opener(
        {
            MATCHDAY_URL: f'<a href="{MATCH_URL}">match</a>'.encode(),
            LINEUP_URL: _lineup_html(complete=True),
        }
    )
    result = fetch_bundesliga_lineups(
        [
            {
                "id": "espn:1",
                "competition_id": "bundesliga",
                "season": "2026",
                "status": "upcoming",
                "kickoff_at": "2026-08-28T18:30:00+00:00",
                "home_team": "Bayern Munich",
                "away_team": "VfB Stuttgart",
            }
        ],
        now=datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
        max_pages=2,
        opener=opener,
    )
    assert result["status"] == "rights_blocked"
    assert result["fixtures"] == []
    assert result["lineups"] == []
    assert result["rights"]["source_id"] == "official_bundesliga_lineups"
