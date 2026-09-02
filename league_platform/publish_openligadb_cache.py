"""Refresh the isolated OpenLigaDB current display in Sites R2.

The script deliberately writes no local snapshot.  It fetches the fixed
OpenLigaDB Bundesliga season endpoint, wraps the response in the closed cache
contract, and sends it to the authenticated Sites route.  The route performs
the final row validation and writes one fixed R2 key.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


OPENLIGADB_HOST = "api.openligadb.de"
CACHE_SCHEMA = "matchline.openligadb.current.cache.v1"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2_000
MAX_RESPONSE_BODY_BYTES = 1024 * 1024
DEFAULT_ENDPOINT = "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/openligadb"
TOKEN_ENV = "MATCHLINE_INGEST_TOKEN"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def current_season(now: datetime | None = None) -> int:
    reference = now or datetime.now(timezone.utc)
    return reference.year if reference.month >= 7 else reference.year - 1


def source_url_for_season(season: int) -> str:
    return f"https://{OPENLIGADB_HOST}/getmatchdata/bl1/{season}"


def normalized_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "matchline-intelligence.willif57kbkd.chatgpt.site"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or parsed.path.rstrip("/") != "/api/v1/openligadb"
    ):
        raise ValueError("OpenLigaDB cache endpoint must be the fixed Sites HTTPS route")
    return urllib.parse.urlunsplit(parsed)


def fetch_payload(season: int, *, timeout: float = 30.0) -> list[object]:
    request = urllib.request.Request(
        source_url_for_season(season),
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "MatchlineOpenLigaDbCache/1.0",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                raise ValueError("OpenLigaDB response exceeds its byte limit")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"OpenLigaDB returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError("OpenLigaDB transport failed") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("OpenLigaDB response exceeds its byte limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenLigaDB response is not JSON") from exc
    if not isinstance(payload, list) or len(payload) > MAX_ROWS:
        raise ValueError("OpenLigaDB response is not a bounded match list")
    return payload


def build_cache_payload(
    payload: Sequence[object],
    *,
    season: int,
    retrieved_at: str | None = None,
) -> bytes:
    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = canonical_json_bytes({
        "schema": CACHE_SCHEMA,
        "season": season,
        "retrievedAt": retrieved,
        "sourceUrl": source_url_for_season(season),
        "payload": list(payload),
    })
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("OpenLigaDB cache upload exceeds its byte limit")
    return body


def upload_cache(
    body: bytes,
    *,
    endpoint: str,
    token: str,
    timeout: float = 60.0,
) -> Mapping[str, object]:
    if not token:
        raise ValueError(f"{TOKEN_ENV} is required")
    request = urllib.request.Request(
        normalized_endpoint(endpoint),
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "MatchlineOpenLigaDbCache/1.0",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BODY_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ValueError(f"OpenLigaDB cache endpoint returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError("OpenLigaDB cache endpoint transport failed") from exc
    if len(response_body) > MAX_RESPONSE_BODY_BYTES:
        raise ValueError("OpenLigaDB cache acknowledgement exceeds its byte limit")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenLigaDB cache acknowledgement is not JSON") from exc
    if not isinstance(result, Mapping) or status != 201 or result.get("status") != "ok":
        raise ValueError("OpenLigaDB cache acknowledgement is invalid")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("MATCHLINE_OPENLIGADB_CACHE_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    season = current_season()
    payload = fetch_payload(season, timeout=args.timeout)
    body = build_cache_payload(payload, season=season)
    result = upload_cache(
        body,
        endpoint=args.endpoint,
        token=os.environ.get(TOKEN_ENV, ""),
        timeout=args.timeout,
    )
    print(json.dumps({
        "status": result.get("status"),
        "schema": result.get("schema"),
        "season": result.get("season"),
        "retrievedAt": result.get("retrievedAt"),
        "rowCount": result.get("rowCount"),
        "sizeBytes": result.get("sizeBytes"),
        "sha256": result.get("sha256"),
        "key": result.get("key"),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
