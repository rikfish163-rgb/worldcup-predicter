"""Small bounded-read helpers for urllib-based public-source adapters.

``urllib.request.urlopen(..., timeout=...)`` limits socket operations, but a
chunked response can keep a source worker alive indefinitely by sending tiny
pieces before each socket timeout.  A source poll needs an overall read
deadline as well as a byte cap so one provider cannot stall the whole
prospective cycle.
"""

from __future__ import annotations

import time
from typing import Any


DEFAULT_RESPONSE_READ_TIMEOUT_SECONDS = 20.0
DEFAULT_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


def _response_socket(response: Any) -> Any | None:
    """Return the underlying socket for a standard ``http.client`` response."""

    # urllib's HTTPResponse shape is response.fp.raw._sock.  Keep the lookup
    # defensive so test doubles and alternate openers can still use the
    # bounded reader without needing to emulate every private attribute.
    buffered = getattr(response, "fp", None)
    raw = getattr(buffered, "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if callable(getattr(sock, "settimeout", None)) else None


def read_response_bounded(
    response: Any,
    max_bytes: int,
    *,
    timeout_seconds: float = DEFAULT_RESPONSE_READ_TIMEOUT_SECONDS,
    chunk_bytes: int = DEFAULT_RESPONSE_READ_CHUNK_BYTES,
) -> bytes:
    """Read a public HTTP response with both byte and wall-clock limits.

    The helper deliberately raises ``TimeoutError``/``OSError`` rather than
    returning a partial payload.  Callers can then record a source-level
    failure and preserve the previous snapshot instead of parsing truncated
    JSON as if it were a valid response.
    """

    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("max_bytes must be non-negative")
    timeout = float(timeout_seconds)
    chunk_limit = max(1, int(chunk_bytes))
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    chunks: list[bytes] = []
    total = 0
    sock = _response_socket(response)
    while total <= limit:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError(f"response read exceeded {timeout:.1f}s")
        if sock is not None:
            # A short socket deadline lets the overall deadline regain
            # control even when a server leaves a chunk open indefinitely.
            try:
                sock.settimeout(min(5.0, remaining))
            except OSError:
                # Some proxies close a chunked response's socket before the
                # HTTPResponse object notices.  Let response.read() surface
                # that as a source-level network error instead of turning the
                # bounded reader itself into an unclassified crash.
                sock = None
        requested = min(chunk_limit, limit + 1 - total)
        chunk = response.read(requested)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("response.read() must return bytes")
        chunk_bytes_value = bytes(chunk)
        chunks.append(chunk_bytes_value)
        total += len(chunk_bytes_value)
        if total > limit:
            break
    return b"".join(chunks)


__all__ = [
    "DEFAULT_RESPONSE_READ_CHUNK_BYTES",
    "DEFAULT_RESPONSE_READ_TIMEOUT_SECONDS",
    "read_response_bounded",
]
