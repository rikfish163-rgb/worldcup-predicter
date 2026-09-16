from __future__ import annotations

import hashlib
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from league_platform.live_sources import openfootball_live
from league_platform.live_sources.openfootball_live import (
    MAX_CONCURRENT_REQUESTS,
    MAX_CONTENT_BYTES,
    OPENFOOTBALL_SOURCES,
    fetch_openfootball_current,
    parse_openfootball_json,
    parse_openfootball_txt,
)
from league_platform.openfootball_raw_archive import OpenFootballRawArchive


PREMIER_LEAGUE_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
SERIE_A_SOURCE_ID = "openfootball:italy:2026-27:1-seriea"


def _source(source_id: str) -> dict:
    return OPENFOOTBALL_SOURCES[source_id]


def test_json_parser_keeps_scores_stable_identity_and_row_lineage():
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = json.dumps(
        {
            "name": "English Premier League 2026/27",
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-21",
                    "time": "20:00",
                    "team1": "Arsenal FC",
                    "team2": "Coventry City FC",
                    "score": {"ft": [2, 1], "ht": [1, 0]},
                },
                {
                    "round": "Matchday 1",
                    "date": "2026-08-22",
                    "team1": "Hull City AFC",
                    "team2": "Manchester United FC",
                    "score": [0, 0],
                },
            ],
        },
        indent=2,
    ).encode()
    observed = datetime(2026, 8, 24, 4, tzinfo=timezone.utc)

    rows = parse_openfootball_json(
        payload,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        retrieved_at=observed,
        url=config["url"],
    )

    assert len(rows) == 2
    result, date_only = rows
    assert result["id"] == "openfootball:premier-league:28c117eaf387ac991474fdeb"
    assert result["provider_fixture_ids"] == {"OpenFootball": result["id"]}
    assert result["kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert result["kickoff_date"] == "2026-08-21"
    assert result["kickoff_time_quality"] == "exact"
    assert result["status"] == "finished"
    assert result["score"] == {"home": 2, "away": 1}
    assert result["halftime_score"] == {"home": 1, "away": 0}
    assert result["result_scope"] == "regulation_90"
    assert date_only["kickoff_date"] == "2026-08-22"
    assert date_only["kickoff_time_quality"] == "date_only"
    assert "kickoff_at" not in date_only
    assert date_only["score"] == {"home": 0, "away": 0}

    expected_hash = hashlib.sha256(payload).hexdigest()
    assert result["source"] == {
        "name": "OpenFootball",
        "source_id": PREMIER_LEAGUE_SOURCE_ID,
        "url": config["url"],
        "retrieved_at": "2026-08-24T04:00:00+00:00",
        "raw_sha256": expected_hash,
        "hash": f"sha256:{expected_hash}",
        "license": "CC0-1.0",
    }
    assert result["lineage"]["source_id"] == PREMIER_LEAGUE_SOURCE_ID
    assert result["lineage"]["record_index"] == 0
    assert result["lineage"]["line_number"] > 0
    assert result["lineage"]["raw_sha256"] == expected_hash
    assert result["lineage"]["license"] == "CC0-1.0"

    amended = json.loads(payload)
    amended["matches"][0]["round"] = "Rescheduled fixture"
    amended["matches"][0]["date"] = "2026-08-23"
    amended["matches"][0]["time"] = "20:30"
    amended["matches"][0]["score"] = {"ft": [3, 1], "ht": [2, 0]}
    amended_rows = parse_openfootball_json(
        json.dumps(amended).encode(),
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        retrieved_at=observed,
        url=config["url"],
    )
    assert amended_rows[0]["id"] == result["id"]


def test_txt_parser_inherits_date_and_group_time_without_inventing_missing_time():
    config = _source(SERIE_A_SOURCE_ID)
    payload = """= Italian Serie A 2026/27
# Date       Sat Aug 22 2026 - Sun May 30 2027 (281d)
▪ Matchday 1
  Sat Aug 22 2026
    18:30  Udinese Calcio          v Como 1907                1-1 (1-0)
           FC Internazionale Milano v AC Monza                 4-1 (1-1)
    20:45  Genoa CFC               v SSC Napoli
           Parma Calcio 1913       v Cagliari Calcio
  Sun Aug 23
           Frosinone Calcio        v Juventus FC
▪ Matchday 17
  Sun Jan 3 2027
    20:45  AS Roma                 v ACF Fiorentina
""".encode()

    rows = parse_openfootball_txt(
        payload,
        source_id=SERIE_A_SOURCE_ID,
        retrieved_at=datetime(2026, 8, 24, 4, tzinfo=timezone.utc),
        url=config["url"],
    )

    assert len(rows) == 6
    assert rows[0]["kickoff_at"] == "2026-08-22T16:30:00+00:00"
    assert rows[1]["kickoff_at"] == rows[0]["kickoff_at"]
    assert rows[1]["kickoff_time_quality"] == "exact"
    assert rows[1]["kickoff_time_source"] == "group_inherited"
    assert rows[1]["score"] == {"home": 4, "away": 1}
    assert rows[1]["halftime_score"] == {"home": 1, "away": 1}
    assert rows[3]["kickoff_at"] == rows[2]["kickoff_at"]
    assert rows[4]["kickoff_time_quality"] == "date_only"
    assert rows[4]["kickoff_date"] == "2026-08-23"
    assert "kickoff_at" not in rows[4]
    assert rows[5]["kickoff_at"] == "2027-01-03T19:45:00+00:00"
    assert rows[0]["lineage"]["line_number"] == 5
    assert rows[1]["lineage"]["line_number"] == 6


def test_timezone_conversion_observes_summer_and_winter_dst():
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-21",
                    "time": "20:00",
                    "team1": "Summer Home",
                    "team2": "Summer Away",
                },
                {
                    "round": "Matchday 20",
                    "date": "2027-01-09",
                    "time": "20:00",
                    "team1": "Winter Home",
                    "team2": "Winter Away",
                },
            ]
        }
    ).encode()

    rows = parse_openfootball_json(
        payload,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        retrieved_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        url=config["url"],
    )

    assert rows[0]["kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert rows[1]["kickoff_at"] == "2027-01-09T20:00:00+00:00"


def test_fetch_contract_quarantines_date_only_and_builds_current_windows():
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-21",
                    "time": "20:00",
                    "team1": "Past Home",
                    "team2": "Past Away",
                    "score": {"ft": [2, 0], "ht": [1, 0]},
                },
                {
                    "round": "Matchday 2",
                    "date": "2026-08-25",
                    "time": "20:00",
                    "team1": "Next Home",
                    "team2": "Next Away",
                },
                {
                    "round": "Matchday 2",
                    "date": "2026-08-28",
                    "time": "20:00",
                    "team1": "Week Home",
                    "team2": "Week Away",
                },
                {
                    "round": "Matchday 2",
                    "date": "2026-08-26",
                    "team1": "Unknown Time Home",
                    "team2": "Unknown Time Away",
                },
            ]
        }
    ).encode()

    class Response:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return config["url"]

        def read(self, _size: int):
            if self._read:
                return b""
            self._read = True
            return payload

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == config["url"]
            assert timeout > 0
            return Response()

    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=Opener(),
    )

    assert result["provider"] == "OpenFootball"
    assert result["retrieved_at"] == "2026-08-24T12:00:00+00:00"
    assert result["status"] == "ok"
    assert result["errors"] == []
    assert len(result["fixtures"]) == 3
    assert len(result["date_only_fixtures"]) == 1
    assert [row["home_team"] for row in result["recent_results"]] == ["Past Home"]
    assert [row["home_team"] for row in result["upcoming_3_days"]] == ["Next Home"]
    assert [row["home_team"] for row in result["upcoming_7_days"]] == [
        "Next Home",
        "Week Home",
    ]
    assert result["source_contract"]["fact_source"] == "OpenFootball"
    assert result["source_contract"]["license"] == "CC0-1.0"
    assert result["source_contract"]["max_response_bytes"] == MAX_CONTENT_BYTES
    assert result["source_contract"]["max_concurrency"] == 4
    assert result["diagnostics"]["date_only_fixture_count"] == 1
    assert result["training_admitted"] is False
    assert result["raw_archive_receipts"] == []
    assert result["source_contract"]["admission_status"] == "candidate_only_unarchived"


def test_fetch_archives_raw_bytes_before_the_real_parser_accepts_rows(tmp_path, monkeypatch):
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = json.dumps(
        {
            "matches": [
                {
                    "date": "2026-08-25",
                    "time": "20:00",
                    "team1": "Archive Home",
                    "team2": "Archive Away",
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    class Response:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return config["url"]

        def read(self, _size: int):
            if self._read:
                return b""
            self._read = True
            return payload

    class Opener:
        def open(self, _request, timeout):
            assert timeout > 0
            return Response()

    events: list[str] = []
    archive = OpenFootballRawArchive(tmp_path / "archive")
    real_parser = openfootball_live.parse_openfootball_json

    def sink(observation):
        assert observation.payload is payload
        assert observation.source_id == PREMIER_LEAGUE_SOURCE_ID
        assert observation.url == config["url"]
        assert observation.source_config() == tuple(sorted(config.items()))
        events.append("sink")
        return archive.store(observation)

    def ordered_parser(*args, **kwargs):
        assert events == ["sink"]
        events.append("parser")
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(openfootball_live, "parse_openfootball_json", ordered_parser)

    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=Opener(),
        raw_sink=sink,
    )

    assert events == ["sink", "parser"]
    assert result["status"] == "ok"
    assert result["training_admitted"] is False
    assert result["source_contract"]["admission_status"] == "raw_observation_archived"
    assert len(result["raw_archive_receipts"]) == 1
    assert result["raw_archive_receipts"][0]["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["fixtures"][0]["home_team"] == "Archive Home"


def test_fetch_isolates_source_with_zero_rows_when_raw_sink_fails(monkeypatch):
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = b'{"matches":[{"date":"2026-08-25","time":"20:00","team1":"No","team2":"Leak"}]}'

    class Response:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return config["url"]

        def read(self, _size: int):
            if self._read:
                return b""
            self._read = True
            return payload

    class Opener:
        def open(self, _request, timeout):
            assert timeout > 0
            return Response()

    parser_called = False

    def forbidden_parser(*_args, **_kwargs):
        nonlocal parser_called
        parser_called = True
        return []

    def failing_sink(_observation):
        raise OSError("manifest fsync failed")

    monkeypatch.setattr(openfootball_live, "parse_openfootball_json", forbidden_parser)

    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=Opener(),
        raw_sink=failing_sink,
    )

    assert parser_called is False
    assert result["status"] == "unavailable"
    assert result["fixtures"] == []
    assert result["date_only_fixtures"] == []
    assert result["recent_results"] == []
    assert result["upcoming_3_days"] == []
    assert result["upcoming_7_days"] == []
    assert result["raw_archive_receipts"] == []
    assert result["diagnostics"]["archived_source_count"] == 0
    assert result["errors"] == [
        {
            "source_id": PREMIER_LEAGUE_SOURCE_ID,
            "competition_id": config["competition_id"],
            "url": config["url"],
            "stage": "raw_archive",
            "error": "manifest fsync failed",
            "transport_retry_count": 0,
        }
    ]


def test_fetch_rejects_self_attested_raw_sink_receipt_before_parsing(monkeypatch):
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = b'{"matches":[{"date":"2026-08-25","time":"20:00","team1":"No","team2":"Proof"}]}'

    class Response:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return config["url"]

        def read(self, _size: int):
            if self._read:
                return b""
            self._read = True
            return payload

    class Opener:
        def open(self, _request, timeout):
            assert timeout > 0
            return Response()

    parser_called = False

    def forbidden_parser(*_args, **_kwargs):
        nonlocal parser_called
        parser_called = True
        return []

    def forged_sink(observation):
        return {
            "status": "raw_observation_archived",
            "source_id": observation.source_id,
            "retrieved_at": observation.retrieved_at.isoformat(),
            "raw_sha256": hashlib.sha256(observation.payload).hexdigest(),
            "size_bytes": len(observation.payload),
            "observation_id": "0" * 64,
            "record_sha256": "0" * 64,
            "manifest_sha256": "0" * 64,
        }

    monkeypatch.setattr(openfootball_live, "parse_openfootball_json", forbidden_parser)

    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=Opener(),
        raw_sink=forged_sink,
    )

    assert parser_called is False
    assert result["status"] == "unavailable"
    assert result["fixtures"] == []
    assert result["raw_archive_receipts"] == []
    assert result["errors"][0]["stage"] == "raw_archive"
    assert "receipt" in result["errors"][0]["error"]


def test_parser_rejects_non_allowlisted_url_and_oversized_response():
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    with pytest.raises(ValueError, match="allowlisted"):
        parse_openfootball_json(
            b'{"matches": []}',
            source_id=PREMIER_LEAGUE_SOURCE_ID,
            retrieved_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            url="https://example.com/en.1.json",
        )
    with pytest.raises(ValueError, match="4 MiB"):
        parse_openfootball_json(
            b" " * (MAX_CONTENT_BYTES + 1),
            source_id=PREMIER_LEAGUE_SOURCE_ID,
            retrieved_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            url=config["url"],
        )


def test_fetch_rejects_changed_final_url_as_redirect():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.com/redirected.json"

        def read(self, _size: int):
            return b'{"matches": []}'

    class Opener:
        def open(self, request, timeout):
            return Response()

    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=Opener(),
    )

    assert result["status"] == "unavailable"
    assert result["fixtures"] == []
    assert result["errors"][0]["source_id"] == PREMIER_LEAGUE_SOURCE_ID
    assert "redirect" in result["errors"][0]["error"].lower()


def test_fetch_rejects_unknown_sources_and_concurrency_bound_is_four():
    assert MAX_CONCURRENT_REQUESTS == 4
    with pytest.raises(ValueError, match="source_id"):
        fetch_openfootball_current(
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
            source_ids=["openfootball:unknown"],
        )


def test_fetch_retries_one_transient_transport_failure_without_changing_lineage():
    config = _source(PREMIER_LEAGUE_SOURCE_ID)
    payload = json.dumps(
        {
            "matches": [
                {
                    "date": "2026-08-25",
                    "time": "20:00",
                    "team1": "Retry Home",
                    "team2": "Retry Away",
                }
            ]
        }
    ).encode()

    class Response:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return config["url"]

        def read(self, _size: int):
            if self._read:
                return b""
            self._read = True
            return payload

    class Opener:
        calls = 0

        def open(self, _request, timeout):
            assert timeout > 0
            self.calls += 1
            if self.calls == 1:
                raise urllib.error.URLError("transient TLS EOF")
            return Response()

    opener = Opener()
    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=opener,
    )

    assert opener.calls == 2
    assert result["status"] == "ok"
    assert result["errors"] == []
    assert result["diagnostics"]["transport_retry_count"] == 1
    assert result["diagnostics"]["retried_source_count"] == 1
    assert result["fixtures"][0]["source"]["url"] == config["url"]


def test_fetch_does_not_retry_http_policy_failure():
    class Opener:
        calls = 0

        def open(self, request, timeout):
            assert timeout > 0
            self.calls += 1
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )

    opener = Opener()
    result = fetch_openfootball_current(
        now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        opener=opener,
    )

    assert opener.calls == 1
    assert result["status"] == "unavailable"
    assert result["diagnostics"]["transport_retry_count"] == 0
    assert result["diagnostics"]["retried_source_count"] == 0
