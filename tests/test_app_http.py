from __future__ import annotations

import gzip
import io
import json

from league_platform.app import PlatformHandler


class _CaptureHandler:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name] = value

    def end_headers(self) -> None:
        return None


def _send(payload: object, headers: dict[str, str] | None = None) -> _CaptureHandler:
    capture = _CaptureHandler(headers)
    PlatformHandler._send_json(capture, payload)
    return capture


def test_json_response_is_gzip_encoded_when_client_advertises_it() -> None:
    capture = _send({"value": "x" * 8_000}, {"Accept-Encoding": "gzip, deflate"})

    assert capture.status == 200
    assert capture.response_headers["Content-Encoding"] == "gzip"
    assert capture.response_headers["Vary"] == "Accept-Encoding"
    body = gzip.decompress(capture.wfile.getvalue())
    assert json.loads(body) == {"value": "x" * 8_000}


def test_small_json_response_keeps_identity_encoding() -> None:
    capture = _send({"ok": True}, {"Accept-Encoding": "gzip"})

    assert capture.status == 200
    assert "Content-Encoding" not in capture.response_headers
    assert capture.response_headers["Vary"] == "Accept-Encoding"
    assert json.loads(capture.wfile.getvalue()) == {"ok": True}


def test_gzip_quality_zero_is_respected() -> None:
    capture = _send({"value": "x" * 8_000}, {"Accept-Encoding": "gzip;q=0, *;q=0.5"})

    assert capture.status == 200
    assert "Content-Encoding" not in capture.response_headers


def test_matching_etag_returns_not_modified_without_body() -> None:
    first = _send({"value": "x" * 8_000}, {"Accept-Encoding": "gzip"})
    etag = first.response_headers["ETag"]

    second = _send(
        {"value": "x" * 8_000},
        {"Accept-Encoding": "gzip", "If-None-Match": etag},
    )

    assert second.status == 304
    assert second.wfile.getvalue() == b""
    assert second.response_headers["ETag"] == etag
