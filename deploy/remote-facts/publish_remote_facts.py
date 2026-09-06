#!/usr/bin/env python3
"""Publish the VPS facts-only snapshot to the authenticated Sites relay.

This is deliberately opt-in: the collector only writes a private file, and
this command performs one explicit HTTPS POST when an ingest token is present.
It never sends model predictions, odds, or raw provider payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_ENDPOINT = "https://matchline-intelligence.willif57kbkd.chatgpt.site/api/v1/remote-facts"
SCHEMA = "matchline.remote.facts.v1"
# The VPS snapshot includes three bounded OpenFootball history seasons. Keep
# this client-side guard aligned with the Sites relay's upload limit.
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def validate_payload(value: object) -> Mapping[str, object]:
    if not _is_mapping(value):
        raise ValueError("remote_facts_payload_not_object")
    root = value
    required = {
        "schema",
        "retrievedAt",
        "hostname",
        "factsOnly",
        "predictions",
        "odds",
        "sources",
        "openfootball",
        "openligadb",
        "wikidata",
        "metNorway",
    }
    if set(root) != required or root.get("schema") != SCHEMA or root.get("factsOnly") is not True:
        raise ValueError("remote_facts_payload_schema_invalid")
    if root.get("predictions") != [] or root.get("odds") != []:
        raise ValueError("remote_facts_payload_contains_model_fields")
    if not isinstance(root.get("sources"), list) or not isinstance(root.get("openfootball"), Mapping) or not isinstance(root.get("openligadb"), Mapping):
        raise ValueError("remote_facts_payload_sections_invalid")
    if not isinstance(root["openfootball"].get("matches"), list) or not isinstance(root["openligadb"].get("matches"), list):
        raise ValueError("remote_facts_payload_matches_invalid")
    history = root["openfootball"].get("history")
    if history is not None:
        if not isinstance(history, Mapping) or not isinstance(history.get("seasons"), list) or not isinstance(history.get("sources"), list) or not isinstance(history.get("matches"), list):
            raise ValueError("remote_facts_payload_history_invalid")
    return root


def read_payload(path: Path) -> tuple[bytes, Mapping[str, object]]:
    body = path.read_bytes()
    if not body or len(body) > MAX_UPLOAD_BYTES:
        raise ValueError("remote_facts_payload_size_invalid")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("remote_facts_payload_json_invalid") from exc
    validate_payload(value)
    return body, value


def endpoint(value: str) -> str:
    if value != DEFAULT_ENDPOINT:
        raise ValueError("remote_facts_endpoint_not_allowlisted")
    return value


def publish(path: Path, target: str, token: str, timeout: float = 30.0) -> dict[str, object]:
    body, value = read_payload(path)
    token = token.strip()
    if not token:
        raise ValueError("matchline_ingest_token_missing")
    request = urllib.request.Request(
        endpoint(target),
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "MatchlineRemoteFactsPublisher/1.0 (+facts-only)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            response_body = response.read(MAX_UPLOAD_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise RuntimeError(f"remote_facts_publish_http_{exc.code}:{detail[:160]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("remote_facts_publish_transport_failed") from exc
    if status < 200 or status >= 300:
        raise RuntimeError(f"remote_facts_publish_http_{status}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("remote_facts_publish_response_invalid") from exc
    if not isinstance(result, Mapping) or result.get("status") != "ok":
        raise RuntimeError("remote_facts_publish_response_not_ok")
    return {
        "status": "ok",
        "schema": SCHEMA,
        "input": str(path),
        "inputSha256": hashlib.sha256(body).hexdigest(),
        "httpStatus": status,
        "retrievedAt": value["retrievedAt"],
        "openfootball": len(value["openfootball"]["matches"]),
        "openligadb": len(value["openligadb"]["matches"]),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=os.environ.get("MATCHLINE_FACTS_OUTPUT", "/home/ubuntu/matchline-facts/current.json"))
    parser.add_argument("--endpoint", default=os.environ.get("MATCHLINE_REMOTE_FACTS_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--token", default=os.environ.get("MATCHLINE_INGEST_TOKEN"))
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.token or ""
    result = publish(Path(args.input), args.endpoint, token, args.timeout)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
