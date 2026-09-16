import json
from copy import deepcopy
from io import BytesIO
from datetime import datetime, timezone

import pytest

from league_platform.current import _validate_live_snapshot
from league_platform.live_sources.http_utils import read_response_bounded
from league_platform.live_sources.openligadb import (
    fetch_openligadb_live_updates,
    parse_openligadb_match_payload,
    parse_openligadb_payload,
)


def _payload() -> bytes:
    return json.dumps([
        {
            "matchID": 123,
            "matchDateTimeUTC": "2026-08-28T18:30:00Z",
            "lastUpdateDateTime": "2026-08-28T20:25:10Z",
            "leagueSeason": 2026,
            "matchIsFinished": True,
            "team1": {"teamId": 1, "teamName": "Home"},
            "team2": {"teamId": 2, "teamName": "Away"},
            "location": {
                "locationID": 9,
                "locationCity": "Muenchen",
                "locationStadium": "Example Arena",
            },
            "goals": [
                {
                    "goalID": 700,
                    "scoreTeam1": 1,
                    "scoreTeam2": 0,
                    "matchMinute": 31,
                    "goalGetterID": 77,
                    "goalGetterName": "A. Player",
                    "scoringTeamId": 1,
                    "isPenalty": False,
                    "isOwnGoal": False,
                    "isOvertime": False,
                    "comment": None,
                }
            ],
            "matchResults": [
                {"resultTypeID": 1, "pointsTeam1": 1, "pointsTeam2": 0},
                {"resultTypeID": 2, "pointsTeam1": 2, "pointsTeam2": 1},
            ],
        },
        {
            "matchID": 124,
            "matchDateTimeUTC": "2026-09-01T18:30:00Z",
            "leagueSeason": 2026,
            "matchIsFinished": False,
            "team1": {"teamId": 3, "teamName": "Next Home"},
            "team2": {"teamId": 4, "teamName": "Next Away"},
            "matchResults": [],
        },
    ]).encode()


def test_openligadb_parser_keeps_90_minute_and_half_time_scores():
    rows = parse_openligadb_payload(
        _payload(),
        retrieved_at=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
        url="https://api.openligadb.de/getmatchdata/bl1/2026",
    )
    assert len(rows) == 2
    assert rows[0]["score"] == {"home": 2, "away": 1}
    assert rows[0]["halftime_score"] == {"home": 1, "away": 0}
    assert rows[1]["score"] is None
    assert rows[0]["source"]["raw_sha256"]
    assert rows[0]["source"]["share_alike_required"] is True
    assert rows[0]["kickoff_at"] == "2026-08-28T18:30:00+00:00"
    assert rows[0]["provider_updated_at"] == "2026-08-28T20:25:10+00:00"
    assert rows[0]["venue"] == {
        "provider_location_id": "9",
        "city": "Muenchen",
        "stadium": "Example Arena",
    }
    assert rows[0]["goals"] == [{
        "provider_goal_id": "700",
        "home_score": 1,
        "away_score": 0,
        "minute": 31,
        "provider_scorer_id": "77",
        "scorer": "A. Player",
        "provider_scoring_team_id": "1",
        "penalty": False,
        "own_goal": False,
        "overtime": False,
        "comment": None,
    }]


def test_openligadb_single_match_parser_binds_url_and_payload_identity():
    row = json.loads(_payload())[0]
    parsed = parse_openligadb_match_payload(
        json.dumps(row).encode(),
        retrieved_at=datetime(2026, 8, 28, 20, 26, tzinfo=timezone.utc),
        url="https://api.openligadb.de/getmatchdata/123",
        expected_match_id="123",
    )

    assert parsed["provider_match_id"] == "123"
    assert parsed["source"]["url"] == "https://api.openligadb.de/getmatchdata/123"
    assert parsed["source"]["license"] == "ODbL-1.0"

    with pytest.raises(ValueError, match="identity"):
        parse_openligadb_match_payload(
            json.dumps(row).encode(),
            retrieved_at=datetime(2026, 8, 28, 20, 26, tzinfo=timezone.utc),
            url="https://api.openligadb.de/getmatchdata/124",
            expected_match_id="124",
        )


class _MatchResponse(BytesIO):
    def __init__(self, payload: bytes, url: str):
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url


class _MatchOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.urls: list[str] = []

    def open(self, request, timeout: float):
        assert timeout == 20
        self.urls.append(request.full_url)
        return _MatchResponse(self.payload, request.full_url)


def test_openligadb_live_fetch_keeps_canonical_id_and_isolates_provider_identity():
    row = json.loads(_payload())[0]
    opener = _MatchOpener(json.dumps(row).encode())
    target = {
        "id": "openfootball:bundesliga:fixture-1",
        "competition_id": "bundesliga",
        "kickoff_at": "2026-08-28T18:30:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "provider": "OpenLigaDB",
        "provider_match_id": "123",
    }

    result = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 20, 26, tzinfo=timezone.utc),
        opener=opener,
    )

    assert opener.urls == ["https://api.openligadb.de/getmatchdata/123"]
    assert result["errors"] == []
    assert result["network_opened"] is True
    assert result["fixture_updates"][0]["fixture_id"] == target["id"]
    assert result["fixture_updates"][0]["score"] == {"home": 2, "away": 1}
    assert result["incidents"][0]["events"][0]["event_id"] == "openligadb:700"
    assert result["source"]["license"] == "ODbL-1.0"
    assert result["source"]["share_alike_required"] is True
    assert result["fixture_updates"][0]["source"]["share_alike_required"] is True

    mismatch = dict(row)
    mismatch["team1"] = {"teamId": 1, "teamName": "Different"}
    rejected = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 20, 26, tzinfo=timezone.utc),
        opener=_MatchOpener(json.dumps(mismatch).encode()),
    )
    assert rejected["fixture_updates"] == []
    assert rejected["errors"][0]["error_code"] == "identity_mismatch"

    future_opener = _MatchOpener(json.dumps(row).encode())
    future = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc),
        opener=future_opener,
    )
    assert future_opener.urls == []
    assert future["network_opened"] is False
    assert future["errors"][0]["error_code"] == "outside_live_window"

    active_row = dict(row)
    active_row["matchIsFinished"] = False
    active_row["matchResults"] = []
    active_row["lastUpdateDateTime"] = "2026-08-28T18:59:00Z"
    active = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        opener=_MatchOpener(json.dumps(active_row).encode()),
    )
    assert active["fixture_updates"][0]["status"] == "in_progress"
    assert active["fixture_updates"][0]["score"] == {"home": 1, "away": 0}

    unconfirmed_row = dict(active_row)
    unconfirmed_row["goals"] = []
    unconfirmed_row["lastUpdateDateTime"] = None
    unconfirmed = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        opener=_MatchOpener(json.dumps(unconfirmed_row).encode()),
    )
    assert unconfirmed["fixture_updates"][0]["status"] == "in_progress_window"
    assert unconfirmed["fixture_updates"][0]["score"] is None

    metadata_update_only_row = dict(unconfirmed_row)
    metadata_update_only_row["lastUpdateDateTime"] = "2026-08-28T18:59:00Z"
    metadata_update_only = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        opener=_MatchOpener(json.dumps(metadata_update_only_row).encode()),
    )
    assert metadata_update_only["fixture_updates"][0]["status"] == "in_progress_window"
    assert metadata_update_only["fixture_updates"][0]["score"] is None

    bad_clock_row = dict(unconfirmed_row)
    bad_clock_row["lastUpdateDateTime"] = "2026-08-28T20:00:00Z"
    bad_clock = fetch_openligadb_live_updates(
        [target],
        now=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        opener=_MatchOpener(json.dumps(bad_clock_row).encode()),
    )
    assert bad_clock["fixture_updates"] == []
    assert bad_clock["errors"][0]["error_code"] == "provider_time_future"


def test_current_snapshot_rejects_tampered_openligadb_goal_observation():
    observed_at = datetime(2026, 8, 28, 20, 26, tzinfo=timezone.utc)
    match = parse_openligadb_payload(
        _payload(),
        retrieved_at=observed_at,
        url="https://api.openligadb.de/getmatchdata/bl1/2026",
    )[0]
    live = {
        "openligadb": {
            "provider": "OpenLigaDB",
            "matches": [match],
            "errors": [],
        }
    }
    _validate_live_snapshot(live, observed_at, {"bundesliga"})

    tampered = deepcopy(live)
    tampered["openligadb"]["matches"][0]["goals"][0]["home_score"] = -1
    with pytest.raises(ValueError, match="goal"):
        _validate_live_snapshot(tampered, observed_at, {"bundesliga"})


def test_openligadb_parser_rejects_unallowlisted_url():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_openligadb_payload(
            _payload(),
            retrieved_at=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            url="https://example.com/getmatchdata/bl1/2026",
        )


def test_openligadb_parser_requires_timezone_on_observation():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_openligadb_payload(
            _payload(),
            retrieved_at=datetime(2026, 8, 12, 1),
            url="https://api.openligadb.de/getmatchdata/bl1/2026",
        )


class _FakeSocket:
    def __init__(self):
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _FakeRaw:
    def __init__(self, sock: _FakeSocket):
        self._sock = sock


class _FakeBuffered:
    def __init__(self, sock: _FakeSocket):
        self.raw = _FakeRaw(sock)


class _FakeResponse:
    def __init__(self, chunks: list[bytes]):
        self.fp = _FakeBuffered(_FakeSocket())
        self._chunks = iter(chunks)

    def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


def test_bounded_response_reader_caps_bytes_and_sets_socket_deadline():
    response = _FakeResponse([b"abc", b"def", b"g"])
    payload = read_response_bounded(response, 6, timeout_seconds=2, chunk_bytes=3)
    assert payload == b"abcdefg"
    assert response.fp.raw._sock.timeouts


class _StalledResponse(_FakeResponse):
    def read(self, _size: int) -> bytes:
        raise TimeoutError("socket stalled")


def test_bounded_response_reader_does_not_return_partial_payload_on_timeout():
    response = _StalledResponse([])
    with pytest.raises(TimeoutError, match="socket stalled"):
        read_response_bounded(response, 10, timeout_seconds=2)
