import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.sports_lottery_history import (
    SPORTS_LOTTERY_HISTORY_URL,
    fetch_fixed_bonus_history,
    fetch_uniform_match_result,
    parse_fixed_bonus_history,
    parse_uniform_match_result,
)


OBSERVED = datetime(2024, 3, 2, 12, tzinfo=timezone.utc)
CAUSAL_OBSERVED = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)


def test_uniform_result_parser_preserves_date_only_boundary_and_scores():
    payload = json.dumps(
        {
            "value": {
                "matchResult": [
                    {
                        "matchId": 1023793,
                        "matchNumStr": "周五003",
                        "leagueName": "日本职业联赛",
                        "allHomeTeam": "横滨水手",
                        "allAwayTeam": "福冈黄蜂",
                        "matchDate": "2024-03-01",
                        "sectionsNo999": "0:1",
                        "goalLine": "-1",
                        "h": "1.62",
                        "d": "3.60",
                        "a": "4.10",
                    }
                ]
            }
        }
    ).encode()
    rows = parse_uniform_match_result(payload, retrieved_at=OBSERVED)
    assert rows[0]["match_id"] == "1023793"
    assert rows[0]["home_score"] == 0
    assert rows[0]["away_score"] == 1
    assert rows[0]["kickoff_at"] is None
    assert rows[0]["enters_model"] is False
    assert "exact_kickoff" in rows[0]["model_exclusion_reason"]
    assert rows[0]["source"]["source_level"] == "official"


def test_fixed_bonus_history_uses_provider_time_and_requires_exact_kickoff():
    payload = json.dumps(
        {
            "value": {
                "oddsHistory": {
                    "matchId": 1023793,
                    "hhadList": [
                        {
                            "goalLine": "-1",
                            "h": "3.10",
                            "d": "3.22",
                            "a": "1.98",
                            "updateDate": "2024-02-29",
                            "updateTime": "13:55:17",
                        },
                        {
                            "goalLine": "-1",
                            "h": "3.15",
                            "d": "3.25",
                            "a": "1.95",
                            "updateDate": "2024-03-01",
                            "updateTime": "14:11:44",
                        },
                    ],
                }
            }
        }
    ).encode()
    audit_rows = parse_fixed_bonus_history(payload, retrieved_at=OBSERVED)
    assert len(audit_rows) == 2
    assert audit_rows[0]["effective_at"] == "2024-02-29T05:55:17+00:00"
    assert audit_rows[0]["effective_at_source"] == "provider_update_datetime"
    assert audit_rows[0]["enters_model"] is False
    assert sum(audit_rows[0]["probability"].values()) == pytest.approx(1.0)

    kickoff = datetime(2024, 3, 2, 6, tzinfo=timezone.utc)
    joined = parse_fixed_bonus_history(payload, retrieved_at=CAUSAL_OBSERVED, kickoff_at=kickoff)
    assert joined[0]["enters_model"] is True
    assert joined[1]["enters_model"] is True


def test_fixed_bonus_history_never_enters_model_when_first_observed_after_kickoff():
    payload = json.dumps(
        {
            "value": {
                "oddsHistory": {
                    "matchId": 1023793,
                    "hhadList": [
                        {
                            "goalLine": "-1",
                            "h": "3.10",
                            "d": "3.22",
                            "a": "1.98",
                            "updateDate": "2024-02-29",
                            "updateTime": "13:55:17",
                        }
                    ],
                }
            }
        }
    ).encode()
    kickoff = datetime(2024, 3, 1, 6, tzinfo=timezone.utc)
    rows = parse_fixed_bonus_history(payload, retrieved_at=OBSERVED, kickoff_at=kickoff)

    assert rows[0]["effective_at"] < kickoff.isoformat()
    assert rows[0]["observed_at"] > kickoff.isoformat()
    assert rows[0]["enters_model"] is False
    assert rows[0]["model_exclusion_reason"] == "first_observation_not_pre_match"


def test_fixed_bonus_fallback_timestamp_is_audit_only():
    payload = b'{"value":{"oddsHistory":{"matchId":1,"hhadList":[{"h":"2","d":"3","a":"4"}]}}}'
    rows = parse_fixed_bonus_history(payload, retrieved_at=OBSERVED)
    assert rows[0]["effective_at"] == OBSERVED.isoformat()
    assert rows[0]["effective_at_source"] == "observed_at_fallback_no_provider_timestamp"
    assert rows[0]["enters_model"] is False


def test_history_parsers_reject_non_allowlisted_urls():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_uniform_match_result(b"{}", retrieved_at=OBSERVED, url="https://example.com/x")
    with pytest.raises(ValueError, match="allowlisted"):
        parse_fixed_bonus_history(b"{}", retrieved_at=OBSERVED, url="https://example.com/x")


def test_history_parsers_surface_gateway_errors_instead_of_returning_empty_data():
    payload = b'{"errorCode":"567","errorMessage":"blocked","value":{}}'
    with pytest.raises(ValueError, match="gateway error 567"):
        parse_uniform_match_result(payload, retrieved_at=OBSERVED)
    with pytest.raises(ValueError, match="gateway error 567"):
        parse_fixed_bonus_history(payload, retrieved_at=OBSERVED)


def test_history_fetches_keep_fixed_endpoint_and_raw_hashes():
    class Response:
        def __init__(self, url, payload):
            self.url = url
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return self.payload

    class Opener:
        def __init__(self):
            self.urls = []

        def open(self, request, timeout):
            assert timeout == 30
            self.urls.append(request.full_url)
            if request.full_url.startswith(SPORTS_LOTTERY_HISTORY_URL):
                payload = b'{"value":{"matchResult":[]}}'
            else:
                payload = b'{"value":{"oddsHistory":{"matchId":1,"hhadList":[]}}}'
            return Response(request.full_url, payload)

    opener = Opener()
    result = fetch_uniform_match_result("2024-03-01", now=OBSERVED, opener=opener)
    fixed = fetch_fixed_bonus_history(1, now=OBSERVED, opener=opener)
    assert result["matches"] == []
    assert result["status"] == "rights_blocked"
    assert fixed["odds_history"] == []
    assert fixed["status"] == "rights_blocked"
    assert opener.urls == []


def test_history_fetch_retries_transient_http_failure_but_not_access_control():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return SPORTS_LOTTERY_HISTORY_URL

        def read(self, _limit):
            return b'{"value":{"matchResult":[]}}'

    class RetryOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                    request.full_url, 503, "temporary", {}, None
                )
            return Response()

    opener = RetryOpener()
    assert fetch_uniform_match_result("2024-03-01", now=OBSERVED, opener=opener)["matches"] == []
    assert opener.calls == 0


def test_history_fetch_retries_transient_transport_failure():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return SPORTS_LOTTERY_HISTORY_URL

        def read(self, _limit):
            return b'{"value":{"matchResult":[]}}'

    class RetryOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary transport failure")
            return Response()

    opener = RetryOpener()
    assert fetch_uniform_match_result("2024-03-01", now=OBSERVED, opener=opener)["matches"] == []
    assert opener.calls == 0
