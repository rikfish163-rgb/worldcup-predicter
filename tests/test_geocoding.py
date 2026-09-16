from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from league_platform.live_sources.geocoding import (
    fetch_open_meteo_geocoding,
    parse_geocoding_payload,
)


AS_OF = datetime(2026, 8, 12, 0, 25, tzinfo=timezone.utc)
URL = "https://geocoding-api.open-meteo.com/v1/search?name=London&count=5&language=en&format=json"


class _Response:
    def __init__(self, payload: bytes, url: str = URL):
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
        return _Response(self.payload, request.full_url)


class _FlakyOpener(_Opener):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.failures = 1

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary handshake timeout")
        return _Response(self.payload, request.full_url)


def _payload() -> bytes:
    return json.dumps(
        {
            "results": [
                {
                    "name": "London",
                    "city": "London",
                    "country": "United Kingdom",
                    "country_code": "GB",
                    "latitude": 51.5072,
                    "longitude": -0.1276,
                    "timezone": "Europe/London",
                }
            ]
        }
    ).encode()


def _assert_geocoding_block(result: dict, opener: _Opener) -> None:
    assert result["status"] == "rights_blocked"
    assert result["geocodes"] == []
    assert result["errors"] == []
    assert result["network_opened"] is False
    assert result["rights"]["source_id"] == "open_meteo_geocoding"
    assert opener.calls == []


def test_parser_requires_exact_city_and_keeps_provenance():
    payload = _payload()
    result = parse_geocoding_payload(
        payload,
        fixture_id="espn:1",
        venue_key=("London", "England", "GB"),
        retrieved_at=AS_OF,
        url=URL,
    )
    assert result["latitude"] == 51.5072
    assert result["longitude"] == -0.1276
    assert result["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()


def test_geocoding_rights_gate_precedes_venue_deduplication():
    opener = _Opener(_payload())
    result = fetch_open_meteo_geocoding(
        [
            {"id": "one", "venue": {"city": "London", "country_code": "GB"}},
            {"id": "two", "venue": {"city": "London", "country_code": "GB"}},
            {"id": "missing", "venue": {}},
        ],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)


def test_geocoding_rejects_non_allowlisted_url_or_mismatched_city():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_geocoding_payload(
            _payload(),
            fixture_id="bad",
            venue_key=("London", "England", "GB"),
            retrieved_at=AS_OF,
            url="https://example.invalid/v1/search",
        )
    with pytest.raises(ValueError, match="no exact venue city"):
        parse_geocoding_payload(
            _payload(),
            fixture_id="bad",
            venue_key=("Manchester", "England", "GB"),
            retrieved_at=AS_OF,
            url=URL,
        )


def test_geocoding_rights_gate_precedes_country_scoped_aliases():
    payload = json.dumps(
        {
            "results": [
                {
                    "name": "Seville",
                    "city": "Seville",
                    "country": "Spain",
                    "country_code": "ES",
                    "latitude": 37.38283,
                    "longitude": -5.97317,
                }
            ]
        }
    ).encode()
    opener = _Opener(payload)
    result = fetch_open_meteo_geocoding(
        [{"id": "sevilla", "venue": {"city": "Sevilla", "country": "Spain"}}],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)


def test_geocoding_rights_gate_precedes_venue_country_normalization():
    payload = json.dumps(
        {
            "results": [
                {
                    "name": "Monaco",
                    "country": "Monaco",
                    "country_code": "MC",
                    "latitude": 43.73718,
                    "longitude": 7.42145,
                }
            ]
        }
    ).encode()
    opener = _Opener(payload)
    result = fetch_open_meteo_geocoding(
        [
            {
                "id": "monaco",
                "venue": {
                    "name": "Stade Louis II",
                    "city": "Monaco",
                    "country": "France",
                },
            }
        ],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)


def test_geocoding_rights_gate_precedes_stadium_label_normalization():
    payload = json.dumps(
        {
            "results": [
                {
                    "name": "Shenyang",
                    "city": "Shenyang",
                    "country": "China",
                    "country_code": "CN",
                    "latitude": 41.79222,
                    "longitude": 123.43278,
                }
            ]
        }
    ).encode()
    opener = _Opener(payload)
    result = fetch_open_meteo_geocoding(
        [
            {
                "id": "liaoning",
                "venue": {
                    "name": "Tiexi New District Sports Centre",
                    "city": "Liaoning",
                    "country": "China PR",
                },
            }
        ],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)


def test_geocoding_rights_gate_precedes_bounded_concurrency():
    payload = json.dumps(
        {
            "results": [
                {
                    "name": "London",
                    "city": "London",
                    "country": "United Kingdom",
                    "country_code": "GB",
                    "latitude": 51.5072,
                    "longitude": -0.1276,
                },
                {
                    "name": "Seville",
                    "city": "Seville",
                    "country": "Spain",
                    "country_code": "ES",
                    "latitude": 37.38283,
                    "longitude": -5.97317,
                },
            ]
        }
    ).encode()
    opener = _Opener(payload)
    result = fetch_open_meteo_geocoding(
        [
            {"id": "first", "venue": {"city": "London", "country_code": "GB"}},
            {"id": "second", "venue": {"city": "Sevilla", "country": "Spain"}},
        ],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)


def test_geocoding_rights_gate_precedes_transport_retry():
    opener = _FlakyOpener(_payload())
    result = fetch_open_meteo_geocoding(
        [{"id": "retry", "venue": {"city": "London", "country_code": "GB"}}],
        now=AS_OF,
        opener=opener,
    )
    _assert_geocoding_block(result, opener)
