from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.lazq import (
    LAZQ_URL,
    fetch_lazq_matches,
    parse_lazq_payload,
)


AS_OF = datetime(2026, 8, 12, 0, 20, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: bytes, url: str = LAZQ_URL):
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
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return _Response(self.payload)


def _payload() -> bytes:
    return json.dumps(
        {
            "status": 0,
            "msg": "",
            "data": [
                {
                    "gameId": "game-1",
                    "jcId": 123,
                    "matchNumStr": "周三001",
                    "competitionName": "中超",
                    "homeTeamName": "主队",
                    "awayTeamName": "客队",
                    "matchTime": 1786561200,
                    "had": {"h": "2.00", "d": "3.00", "a": "4.00"},
                    "hhad": {"h": "1.80", "d": "3.20", "a": "4.20", "goal": "-1"},
                    "ttg": {
                        "s0": "4.00",
                        "s1": "3.00",
                        "s2": "2.50",
                        "s3": "4.00",
                        "s4": "6.00",
                        "s5": "10.00",
                        "s6": "20.00",
                        "s7": "30.00",
                    },
                    "hafu": {
                        key: "5.00"
                        for key in ("hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa")
                    },
                }
            ],
        }
    ).encode()


def test_parser_keeps_lottery_markets_and_marks_mirror_unverified():
    payload = _payload()
    rows = parse_lazq_payload(payload, retrieved_at=AS_OF)
    assert len(rows) == 1
    row = rows[0]
    assert row["competition"] == "中超"
    assert row["hhad_line"] == "-1"
    assert set(row["had_probability"]) == {"h", "d", "a"}
    assert abs(sum(row["had_probability"].values()) - 1) < 1e-8
    assert row["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["source"]["official_verified"] is False
    assert row["source"]["purchase_eligible"] is False
    assert row["source"]["model_eligible"] is False


def test_fetch_posts_only_the_page_owned_public_shape():
    opener = _Opener(_payload())
    result = fetch_lazq_matches(now=AS_OF, opener=opener)
    assert result["status"] == "rights_blocked"
    assert result["matches"] == []
    assert result["rights"]["source_id"] == "lazq_public_mirror"
    assert opener.calls == []


def test_parser_rejects_non_allowlisted_url_and_invalid_response():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_lazq_payload(_payload(), retrieved_at=AS_OF, url="https://example.invalid/api")
    with pytest.raises(ValueError, match="no public match list"):
        parse_lazq_payload(b'{"status":1,"data":[]}', retrieved_at=AS_OF)
