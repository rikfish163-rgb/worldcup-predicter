"""Upload verified OpenFootball raw evidence to the Sites R2/D1 archive.

The local durable archive remains the producer trust boundary.  This module
revalidates every manifest row and raw object before constructing bounded,
content-addressed uploads.  It never treats a successful raw upload as a model
maturity or prediction-publication receipt.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
)
from league_platform.openfootball_raw_archive import (
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    MAX_RAW_BYTES,
    load_verified_openfootball_archive,
)


RAW_UPLOAD_SCHEMA = "matchline.raw_artifact_upload.v1"
RAW_ATTESTATION_SCHEMA = "matchline.raw_artifact_attestation.v1"
PRODUCER_NAME = "matchline-python-publisher"
PRODUCER_VERSION = "v260"
MAX_UPLOAD_BYTES = 7 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_HTTP_ERROR_DETAIL_CHARS = 512
MAX_HTTP_ERROR_FIELD_CHARS = 160
ATTESTATION_TTL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MatchlineRawArchive/1.0"
)
SITES_BYPASS_TOKEN_ENV = "MATCHLINE_SITES_BYPASS_TOKEN"
PUBLISH_USER_AGENT_ENV = "MATCHLINE_PUBLISH_USER_AGENT"
RAW_ENDPOINT_ENV = "MATCHLINE_RAW_ARTIFACT_ENDPOINT"
INGEST_TOKEN_ENV = "MATCHLINE_INGEST_TOKEN"
PRODUCER_KEY_ID_ENV = "MATCHLINE_PRODUCER_KEY_ID"
PRODUCER_SIGNING_SECRET_ENV = "MATCHLINE_PRODUCER_SIGNING_SECRET"
_SHA256 = set("0123456789abcdef")

RawSender = Callable[..., Mapping[str, Any]]


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


def _safe_http_error_detail(
    error: urllib.error.HTTPError,
    *,
    secret_values: Sequence[str],
) -> str | None:
    """Extract bounded server diagnostics without echoing request credentials."""

    try:
        raw = error.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_RESPONSE_BYTES:
        return "body_truncated"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None

    details: dict[str, str] = {}
    for key in ("error", "reason", "policyVersion"):
        item = value.get(key)
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or any(secret and secret in item for secret in secret_values):
            continue
        if not all(char.isascii() and (char.isalnum() or char in "_.:-") for char in item):
            continue
        if len(item) > MAX_HTTP_ERROR_FIELD_CHARS:
            item = item[: MAX_HTTP_ERROR_FIELD_CHARS - 3] + "..."
        details[key] = item
    if not details:
        return None
    return json.dumps(
        details,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )[:MAX_HTTP_ERROR_DETAIL_CHARS]


def canonical_json_bytes(value: object) -> bytes:
    """Return the same canonical JSON bytes used by the Sites verifier."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("raw artifact value is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and set(value) <= _SHA256
    )


def _read_regular(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"cannot open {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > max_bytes:
            raise ValueError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds its size limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            raise ValueError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_durable_archive(root: Path | str) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ValueError("OpenFootball raw archive must use a durable absolute path")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(Path("/dev/shm").resolve(strict=True))
    except ValueError:
        pass
    except OSError as exc:
        raise ValueError("OpenFootball raw archive durable path is unavailable") from exc
    else:
        raise ValueError("OpenFootball raw archive must be durable, not /dev/shm")
    try:
        status = candidate.lstat()
    except OSError as exc:
        raise ValueError("OpenFootball raw archive durable path is unavailable") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ValueError("OpenFootball raw archive must be a durable directory")
    return candidate


def _manifest_records(manifest_bytes: bytes) -> list[tuple[dict[str, Any], str]]:
    if not manifest_bytes or not manifest_bytes.endswith(b"\n"):
        raise ValueError("OpenFootball raw manifest must be a non-empty canonical JSONL file")
    records: list[tuple[dict[str, Any], str]] = []
    prefix = bytearray()
    for line_number, line in enumerate(manifest_bytes.splitlines(keepends=True), start=1):
        if line == b"\n":
            raise ValueError(f"OpenFootball raw manifest has a blank line at {line_number}")
        prefix.extend(line)
        try:
            decoded = line[:-1].decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"OpenFootball raw manifest is invalid at line {line_number}"
            ) from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) != line[:-1]:
            raise ValueError(f"OpenFootball raw manifest is not canonical at line {line_number}")
        records.append((value, _sha256(bytes(prefix))))
    return records


def _safe_raw_object(root: Path, record: Mapping[str, Any]) -> bytes:
    raw_path = record.get("raw_path")
    raw_sha256 = record.get("raw_sha256")
    size_bytes = record.get("size_bytes")
    if not isinstance(raw_path, str) or not _valid_sha256(raw_sha256):
        raise ValueError("OpenFootball raw record identity is invalid")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("OpenFootball raw record size is invalid")
    member = root / raw_path
    try:
        member.relative_to(root)
    except ValueError as exc:
        raise ValueError("OpenFootball raw object escapes its archive root") from exc
    raw = _read_regular(member, max_bytes=MAX_RAW_BYTES, label="OpenFootball raw object")
    if len(raw) != size_bytes or _sha256(raw) != raw_sha256:
        raise ValueError("OpenFootball raw object does not match its manifest")
    return raw


def _source_policy_id(record: Mapping[str, Any]) -> str:
    source_id = record.get("source_id")
    if source_id in OPENFOOTBALL_CURRENT_SOURCE_IDS:
        return "openfootball_current"
    return "openfootball_historical"


def build_raw_artifact_uploads(root: Path | str) -> list[dict[str, Any]]:
    """Revalidate a local archive and group all observations by raw object."""

    archive_root = Path(root)
    manifest_path = archive_root / MANIFEST_NAME
    manifest_bytes = _read_regular(
        manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        label="OpenFootball raw manifest",
    )
    records_with_prefix = _manifest_records(manifest_bytes)
    source_ids = sorted({str(record.get("source_id") or "") for record, _ in records_with_prefix})
    if not source_ids or "" in source_ids:
        raise ValueError("OpenFootball raw manifest source inventory is invalid")
    verified = load_verified_openfootball_archive(
        archive_root,
        source_ids=source_ids,
        observed_before=datetime.max.replace(tzinfo=timezone.utc),
    )
    manifest_sha256 = _sha256(manifest_bytes)
    if verified.get("manifest_sha256") != manifest_sha256:
        raise ValueError("OpenFootball raw manifest changed during verification")

    grouped: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for record, prefix_sha256 in records_with_prefix:
        raw_sha256 = record.get("raw_sha256")
        if not _valid_sha256(raw_sha256):
            raise ValueError("OpenFootball raw manifest contains an invalid digest")
        grouped[str(raw_sha256)].append((record, prefix_sha256))

    uploads: list[dict[str, Any]] = []
    for raw_sha256 in sorted(grouped):
        observations = grouped[raw_sha256]
        first_record = observations[0][0]
        raw = _safe_raw_object(archive_root, first_record)
        for record, _ in observations[1:]:
            if record.get("size_bytes") != len(raw):
                raise ValueError("OpenFootball shared raw object has conflicting sizes")
        parser_contracts = {record.get("parser_contract") for record, _ in observations}
        if parser_contracts == {"openfootball_json_v1"}:
            content_type = "application/json"
        elif parser_contracts == {"openfootball_football_txt_v1"}:
            content_type = "text/plain"
        elif parser_contracts <= {"openfootball_json_v1", "openfootball_football_txt_v1"}:
            content_type = "application/octet-stream"
        else:
            raise ValueError("OpenFootball raw parser contract is invalid")
        upload = {
            "schemaVersion": RAW_UPLOAD_SCHEMA,
            "archiveManifestSha256": manifest_sha256,
            "raw": {
                "sha256": raw_sha256,
                "sizeBytes": len(raw),
                "contentType": content_type,
                "base64": base64.b64encode(raw).decode("ascii"),
            },
            "observations": [
                {
                    "archiveRecord": record,
                    "manifestPrefixSha256": prefix_sha256,
                }
                for record, prefix_sha256 in observations
            ],
        }
        if len(canonical_json_bytes(upload)) > MAX_UPLOAD_BYTES:
            raise ValueError("raw artifact upload exceeds the Sites request limit")
        uploads.append(upload)
    return uploads


def _normalized_endpoint(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError("raw artifact endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("raw artifact endpoint must be a credential-free HTTPS URL")
    return urllib.parse.urlunsplit(parsed)


def raw_artifact_attestation_headers(
    body: bytes,
    *,
    endpoint: str,
    key_id: str,
    signing_secret: str,
    source_policy_ids: Sequence[str],
    archive_manifest_sha256: str,
    stream: str = "raw_artifact",
    issued_at: datetime | None = None,
) -> dict[str, str]:
    """Sign one raw-backed upload without depending on model maturity.

    The same archive identity can authorize the immutable raw object stream or
    a compact research fixture snapshot derived from a verified replay.  The
    stream is closed here so callers cannot mint arbitrary write capabilities.
    """

    normalized_key = key_id.strip()
    secret = signing_secret.encode("utf-8")
    if not normalized_key or len(normalized_key) > 120 or len(secret) < 32 or len(secret) > 4096:
        raise ValueError("raw artifact producer signing identity is invalid")
    source_ids = sorted({str(value).strip() for value in source_policy_ids if str(value).strip()})
    if not source_ids or any(
        value not in {"openfootball_current", "openfootball_historical"} for value in source_ids
    ):
        raise ValueError("raw artifact source policy identity is invalid")
    if not _valid_sha256(archive_manifest_sha256) or archive_manifest_sha256 == "0" * 64:
        raise ValueError("raw artifact manifest digest is invalid")
    if stream not in {"raw_artifact", "research_fixture_snapshot"}:
        raise ValueError("raw-backed producer stream is invalid")
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires = (
        (now + timedelta(seconds=ATTESTATION_TTL_SECONDS))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    attestation = {
        "archiveManifestSha256": archive_manifest_sha256,
        "bodySha256": _sha256(body),
        "destination": _normalized_endpoint(endpoint),
        "expiresAt": expires,
        "issuedAt": issued,
        "keyId": normalized_key,
        "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
        "rightsUseCase": "redistribution",
        "schemaVersion": RAW_ATTESTATION_SCHEMA,
        "sourcePolicyIds": source_ids,
        "stream": stream,
    }
    canonical = canonical_json_bytes(attestation)
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return {
        "X-Matchline-Producer-Attestation": encoded,
        "X-Matchline-Producer-Signature": f"v1={signature}",
    }


def validate_raw_artifact_ack(
    upload: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the complete endpoint acknowledgement before counting success."""

    expected_keys = {
        "status",
        "acceptedObservations",
        "createdObject",
        "createdObservations",
        "duplicateObservations",
        "rawSha256",
        "objectKey",
        "observationSetSha256",
        "archiveManifestSha256",
    }
    if not isinstance(response, Mapping) or set(response) != expected_keys:
        raise ValueError("raw artifact response contract is invalid")
    raw = upload.get("raw")
    observations = upload.get("observations")
    if not isinstance(raw, Mapping) or not isinstance(observations, list):
        raise ValueError("raw artifact upload contract is invalid")
    raw_sha256 = raw.get("sha256")
    observation_ids = sorted(str(item["archiveRecord"]["observation_id"]) for item in observations)
    accepted = len(observation_ids)
    created_object = response.get("createdObject")
    created_observations = response.get("createdObservations")
    duplicate_observations = response.get("duplicateObservations")
    object_key = f"openfootball/raw/sha256/{str(raw_sha256)[:2]}/{raw_sha256}.raw"
    valid_counts = (
        isinstance(created_object, int)
        and not isinstance(created_object, bool)
        and created_object in {0, 1}
        and isinstance(created_observations, int)
        and not isinstance(created_observations, bool)
        and created_observations >= 0
        and isinstance(duplicate_observations, int)
        and not isinstance(duplicate_observations, bool)
        and duplicate_observations >= 0
        and created_observations + duplicate_observations == accepted
    )
    if not valid_counts or any(
        (
            response.get("status") != "ok",
            response.get("acceptedObservations") != accepted,
            response.get("rawSha256") != raw_sha256,
            response.get("objectKey") != object_key,
            response.get("observationSetSha256") != _sha256(canonical_json_bytes(observation_ids)),
            response.get("archiveManifestSha256") != upload.get("archiveManifestSha256"),
        )
    ):
        raise ValueError("raw artifact response contract is invalid")
    return dict(response)


def _send_raw_upload(
    *,
    endpoint: str,
    body: bytes,
    headers: Mapping[str, str],
    token: str,
    timeout: float,
) -> Mapping[str, Any]:
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": os.environ.get(PUBLISH_USER_AGENT_ENV, DEFAULT_USER_AGENT),
        **dict(headers),
    }
    bypass = os.environ.get(SITES_BYPASS_TOKEN_ENV, "").strip()
    if bypass:
        request_headers["OAI-Sites-Authorization"] = f"Bearer {bypass}"
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=request_headers,
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _safe_http_error_detail(
            exc,
            secret_values=(token, bypass),
        )
        suffix = f": response={detail}" if detail else ""
        raise ValueError(
            f"raw artifact endpoint returned HTTP {exc.code}{suffix}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError("raw artifact endpoint transport failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("raw artifact response exceeded 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw artifact response is not JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("raw artifact response must be an object")
    return value


def upload_openfootball_raw_archive(
    root: Path | str,
    *,
    endpoint: str,
    token: str,
    key_id: str,
    signing_secret: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sender: RawSender | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload every verified observation; server idempotency makes retries safe."""

    archive_root = _require_durable_archive(root)
    normalized_endpoint = _normalized_endpoint(endpoint)
    uploads = build_raw_artifact_uploads(archive_root)
    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "requests": 0,
        "rawObjects": len(uploads),
        "observations": sum(len(upload["observations"]) for upload in uploads),
        "rawBytes": sum(int(upload["raw"]["sizeBytes"]) for upload in uploads),
        "createdObjects": 0,
        "createdObservations": 0,
        "duplicateObservations": 0,
        "archiveManifestSha256": uploads[0]["archiveManifestSha256"] if uploads else None,
    }
    if dry_run:
        return summary
    if not token:
        raise ValueError("raw artifact ingest token is required")
    send = sender or _send_raw_upload
    for upload in uploads:
        body = canonical_json_bytes(upload)
        source_policy_ids = sorted(
            {_source_policy_id(item["archiveRecord"]) for item in upload["observations"]}
        )
        attestation_headers = raw_artifact_attestation_headers(
            body,
            endpoint=normalized_endpoint,
            key_id=key_id,
            signing_secret=signing_secret,
            source_policy_ids=source_policy_ids,
            archive_manifest_sha256=str(upload["archiveManifestSha256"]),
        )
        response = send(
            endpoint=normalized_endpoint,
            body=body,
            headers=attestation_headers,
            token=token,
            timeout=timeout,
        )
        ack = validate_raw_artifact_ack(upload, response)
        summary["requests"] += 1
        summary["createdObjects"] += int(ack["createdObject"])
        summary["createdObservations"] += int(ack["createdObservations"])
        summary["duplicateObservations"] += int(ack["duplicateObservations"])
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--endpoint", default=os.environ.get(RAW_ENDPOINT_ENV))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    endpoint = (args.endpoint or "").strip()
    if not endpoint:
        raise ValueError(f"--endpoint or {RAW_ENDPOINT_ENV} is required")
    result = upload_openfootball_raw_archive(
        args.archive_root,
        endpoint=endpoint,
        token=os.environ.get(INGEST_TOKEN_ENV, ""),
        key_id=os.environ.get(PRODUCER_KEY_ID_ENV, ""),
        signing_secret=os.environ.get(PRODUCER_SIGNING_SECRET_ENV, ""),
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_raw_artifact_uploads",
    "canonical_json_bytes",
    "main",
    "raw_artifact_attestation_headers",
    "upload_openfootball_raw_archive",
    "validate_raw_artifact_ack",
]
