"""Audit local Matchline evidence against a public Sites read model.

The audit is deliberately read-only.  It does not publish, retry protected
endpoints, or use an ingest token.  A public site is considered in parity only
when its prospective gate exposes the same model lock and a snapshot that is
not older than the local snapshot by more than the configured lag budget.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from league_platform.site_preflight import probe_public_site


DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_LAG_SECONDS = 30 * 60


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bounded_json(response: Any, *, max_bytes: int) -> object:
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response exceeds audit size limit")
    return json.loads(data.decode("utf-8"))


def fetch_json(url: str, *, timeout: float = 20.0, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Fetch one public JSON endpoint without credentials or broad retries."""

    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return {"status": "blocked_url_policy", "url": url, "error": "public audit requires HTTPS"}
    request = Request(url, headers={"accept": "application/json", "user-agent": "MatchlinePublicationAudit/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            final = urlparse(final_url)
            if final.hostname != parsed.hostname:
                return {"status": "redirect_host_mismatch", "url": url, "final_url": final_url}
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and final.scheme != parsed.scheme:
                return {"status": "redirect_scheme_mismatch", "url": url, "final_url": final_url}
            payload = _bounded_json(response, max_bytes=max_bytes)
            if not isinstance(payload, Mapping):
                return {"status": "malformed", "url": url, "final_url": final_url, "error": "JSON root is not an object"}
            return {"status": "ok", "http_status": getattr(response, "status", 200), "url": url, "final_url": final_url, "payload": dict(payload)}
    except HTTPError as exc:
        return {"status": "http_error", "http_status": exc.code, "url": url, "error": str(exc.reason)}
    except (OSError, URLError, TimeoutError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "url": url, "error": str(exc)}


def _local_state(snapshot: Mapping[str, Any], lock: Mapping[str, Any]) -> dict[str, Any]:
    as_of = _text(snapshot.get("as_of"))
    model_hash = _text(lock.get("model_version_sha256"))
    return {
        "as_of": as_of,
        "as_of_valid": _utc(as_of) is not None,
        "model_version_sha256": model_hash,
        "production_ready": snapshot.get("production_ready") is True,
        "snapshot_schema": _text(snapshot.get("schema_version")),
    }


def _source_summary(public_sources: Mapping[str, Any] | None) -> dict[str, Any]:
    if public_sources is None:
        return {"status": "not_checked", "run_count": None, "latest_finished_at": None, "providers": []}
    rows = public_sources.get("runs")
    if not isinstance(rows, list):
        return {"status": "malformed", "run_count": None, "latest_finished_at": None, "providers": []}
    providers: list[dict[str, Any]] = []
    latest: datetime | None = None
    for row in rows[:100]:
        item = _mapping(row)
        provider = _text(item.get("provider"))
        status = _text(item.get("status"))
        finished = _text(item.get("finishedAt"))
        finished_at = _utc(finished)
        if finished_at is not None and (latest is None or finished_at > latest):
            latest = finished_at
        if provider or status:
            providers.append({"provider": provider, "status": status, "finished_at": finished})
    return {
        "status": "ok",
        "run_count": len(rows),
        "latest_finished_at": latest.isoformat() if latest else None,
        "providers": providers[:20],
    }


def build_publication_audit(
    *,
    snapshot: Mapping[str, Any],
    lock: Mapping[str, Any],
    public_status: Mapping[str, Any] | None,
    public_sources: Mapping[str, Any] | None = None,
    public_status_request: Mapping[str, Any] | None = None,
    public_sources_request: Mapping[str, Any] | None = None,
    public_preflight: Mapping[str, Any] | None = None,
    checked_at: datetime | None = None,
    max_lag_seconds: int = DEFAULT_MAX_LAG_SECONDS,
) -> dict[str, Any]:
    """Compare local and public read-model metadata without changing either."""

    reference = (checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = _local_state(snapshot, lock)
    public = _mapping(public_status)
    gate = _mapping(public.get("gate"))
    local_at = _utc(local.get("as_of"))
    public_as_of = _text(public.get("asOf"))
    public_at = _utc(public_as_of)
    local_hash = _text(local.get("model_version_sha256"))
    public_hash = _text(gate.get("modelVersionSha256"))

    errors: list[str] = []
    request_status = _text(_mapping(public_status_request).get("status")) if public_status_request else None
    if public_status_request is not None and request_status != "ok":
        errors.append(f"public_status:{request_status or 'missing'}")
    if public_status_request is None and not public_status:
        errors.append("public_status:missing")
    if public_status_request is not None and request_status == "ok" and not public:
        errors.append("public_status:malformed")
    if local_at is None:
        errors.append("local_as_of:invalid")
    if public_at is None:
        errors.append("public_as_of:invalid")
    if not local_hash:
        errors.append("local_model_hash:missing")
    if not public_hash:
        errors.append("public_model_hash:missing")

    lag_seconds: int | None = None
    if local_at is not None and public_at is not None:
        lag_seconds = int((local_at - public_at).total_seconds())
        if lag_seconds > max_lag_seconds:
            errors.append("public_snapshot_behind_local")
        elif lag_seconds < -max_lag_seconds:
            errors.append("public_snapshot_ahead_of_local")

    hash_match = bool(local_hash and public_hash and local_hash.lower() == public_hash.lower())
    if local_hash and public_hash and not hash_match:
        errors.append("model_lock_mismatch")

    source_summary = _source_summary(public_sources)
    if public_sources_request is not None and _text(_mapping(public_sources_request).get("status")) != "ok":
        errors.append(f"public_sources:{_text(_mapping(public_sources_request).get('status')) or 'missing'}")
    if public_sources_request is not None and _text(_mapping(public_sources_request).get("status")) == "ok" and source_summary["status"] != "ok":
        errors.append("public_sources:malformed")

    preflight = _mapping(public_preflight)
    preflight_status = _text(preflight.get("status")) if public_preflight is not None else None
    contract_routes_ok = (
        preflight.get("all_expected_routes_ok") is True
        if public_preflight is not None
        else None
    )
    if public_preflight is not None and (preflight_status != "ready_for_data_review" or contract_routes_ok is not True):
        errors.append(f"public_routes:{preflight_status or 'missing'}")
    failed_routes = [
        _text(_mapping(route).get("path"))
        for route in (preflight.get("routes") if isinstance(preflight.get("routes"), list) else [])[:30]
        if _mapping(route).get("status_code") != 200 and _text(_mapping(route).get("path"))
    ]

    public_mode = _text(public.get("mode")) or _text(public.get("status")) or "unknown"
    production_allowed = gate.get("productionAllowed") is True and public.get("productionReady") is True
    parity = not errors and hash_match and lag_seconds is not None and abs(lag_seconds) <= max_lag_seconds
    return {
        "schema_version": "matchline.publication_audit.v1",
        "checked_at": reference.isoformat(),
        "status": "pass" if parity else "blocked",
        "public_url": _text(public.get("publicUrl")),
        "max_lag_seconds": max_lag_seconds,
        "errors": sorted(set(errors)),
        "checks": {
            "public_read_model_reachable": public_status_request is None or _text(_mapping(public_status_request).get("status")) == "ok",
            "model_lock_match": hash_match,
            "snapshot_time_comparable": local_at is not None and public_at is not None,
            "snapshot_lag_seconds": lag_seconds,
            "snapshot_within_lag_budget": lag_seconds is not None and abs(lag_seconds) <= max_lag_seconds,
            "production_allowed_by_public_gate": production_allowed,
            "public_contract_routes_ok": contract_routes_ok,
        },
        "local": local,
        "public": {
            "mode": public_mode,
            "status": _text(public.get("status")),
            "generated_at": _text(public.get("generatedAt")),
            "as_of": public_as_of,
            "model_version_sha256": public_hash,
            "gate_status": _text(gate.get("status")),
            "production_ready": public.get("productionReady") is True,
            "production_allowed": gate.get("productionAllowed") is True,
        },
        "public_sources": source_summary,
        "public_preflight": {
            "status": preflight_status,
            "reachable": preflight.get("reachable") is True if public_preflight is not None else None,
            "deployment_ready": preflight.get("deployment_ready") is True if public_preflight is not None else None,
            "all_expected_routes_ok": contract_routes_ok,
            "failed_routes": failed_routes[:20],
        },
        "deployment_required": not parity,
        "interpretation": (
            "Public read model matches the local model lock and snapshot time budget."
            if parity
            else "Do not call the public site current; publish or repair the read model before presenting it as live."
        ),
    }


def _load_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} root must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--snapshot", type=Path, default=Path("data/live/current.json"))
    parser.add_argument("--lock", type=Path, default=Path("docs/evidence/prospective-model-lock-current.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-lag-seconds", type=int, default=DEFAULT_MAX_LAG_SECONDS)
    args = parser.parse_args()

    base = args.public_url.rstrip("/") + "/"
    status_request = fetch_json(urljoin(base, "api/prospective-status"), timeout=args.timeout)
    sources_request = fetch_json(urljoin(base, "api/source-runs?limit=50"), timeout=args.timeout)
    preflight = probe_public_site(base, timeout=args.timeout)
    report = build_publication_audit(
        snapshot=_load_object(args.snapshot),
        lock=_load_object(args.lock),
        public_status=_mapping(status_request.get("payload")),
        public_sources=_mapping(sources_request.get("payload")),
        public_status_request=status_request,
        public_sources_request=sources_request,
        public_preflight=preflight,
        max_lag_seconds=max(0, args.max_lag_seconds),
    )
    report["public_url"] = base.rstrip("/")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "checks": report["checks"]}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_publication_audit", "fetch_json"]
