"""Read-only smoke test for the public Matchline Sites deployment.

This check answers a deployment question that local builds cannot answer:
whether the public worker is serving the expected D1-backed API and whether
the response contains real ledger rows.  It never writes to Sites/D1, sends
credentials, or falls back to the local offline bundle.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 512 * 1024
USER_AGENT = "MatchlineReadiness/1.0 (+https://predict.hetaisheng.ccwu.cc)"

ROUTES: tuple[dict[str, Any], ...] = (
    {"name": "ledger", "path": "/api/ledger", "collection_keys": ()},
    {
        "name": "source_runs",
        "path": "/api/source-runs?limit=5&latestPerProvider=1&auditOnly=1",
        "collection_keys": ("runs",),
    },
    {"name": "upcoming_matches", "path": "/api/matches?scope=upcoming&limit=5", "collection_keys": ("matches",)},
    {"name": "live_matches", "path": "/api/matches?scope=live&limit=5", "collection_keys": ("matches",)},
    {"name": "team_research", "path": "/api/teams", "collection_keys": ("candidates",)},
    {"name": "research_alerts", "path": "/api/alerts?scope=upcoming&limit=5&alertLimit=5", "collection_keys": ("alerts",)},
    {"name": "evaluations", "path": "/api/evaluations", "collection_keys": ()},
    {"name": "prospective_status", "path": "/api/prospective-status", "collection_keys": ()},
    # These are the versioned customer/read-model contracts consumed by the
    # current Sites UI.  Keeping them in the smoke probe prevents an older
    # public deployment from being called ready merely because the legacy
    # routes still answer from D1.
    {"name": "release_status", "path": "/api/release-status", "collection_keys": ()},
    {"name": "api_catalog", "path": "/api/v1/catalog", "collection_keys": ()},
    {"name": "versioned_sources", "path": "/api/v1/sources?limit=5", "collection_keys": ("sources",)},
    {"name": "versioned_matches", "path": "/api/v1/matches?scope=upcoming&limit=5", "collection_keys": ("matches",)},
    {"name": "service_status", "path": "/api/v1/service-status", "collection_keys": ()},
    {"name": "competitive_benchmark", "path": "/api/v1/benchmark", "collection_keys": ()},
    {"name": "live_overlay", "path": "/api/v1/live-overlay", "collection_keys": ()},
)

# API routes prove that the worker has the expected read-model contract.  The
# HTML probes prove that the public edge is serving the same application shell
# and that a single-match page does not get stuck on its client-only loading
# placeholder.  They are deliberately separate from the JSON route list so a
# caller can provide a JSON-only test fetcher without accidentally claiming an
# HTML check passed.
HTML_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "name": "homepage_html",
        "path": "/",
        "required_markers": ("Matchline", "比赛数据台"),
        "forbidden_markers": (),
    },
    {
        "name": "single_match_html",
        # A deliberately unavailable fixture is enough to exercise the route
        # contract.  The current application must render an explicit
        # unavailable state (or a full detail state), never an indefinite
        # client-only loading shell.
        "path": "/matches/61",
        "required_markers": ("Matchline", "detail-page"),
        "forbidden_markers": ("读取单场账本",),
    },
)


def _base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("base URL is required")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("base URL must be an HTTP(S) origin")
    if parsed.username or parsed.password:
        raise ValueError("base URL must not contain credentials")
    return value.strip().rstrip("/") + "/"


def _bounded_json_response(url: str, *, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit public URL input
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(response.getcode())
            content_type = str(response.headers.get("content-type") or "")
            if len(body) > MAX_RESPONSE_BYTES:
                return {
                    "status_code": status,
                    "content_type": content_type,
                    "error_code": "response_too_large",
                }
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {
                    "status_code": status,
                    "content_type": content_type,
                    "error_code": "invalid_json",
                }
            return {
                "status_code": status,
                "content_type": content_type,
                "payload": payload,
            }
    except HTTPError as exc:
        return {
            "status_code": int(exc.code),
            "content_type": str(exc.headers.get("content-type") or "") if exc.headers else "",
            "error_code": f"http_{exc.code}",
        }
    except (TimeoutError, URLError, OSError) as exc:
        return {"status_code": None, "content_type": "", "error_code": "network_error", "error": str(exc)}


def _bounded_html_response(url: str, *, timeout: float) -> dict[str, Any]:
    """Fetch a bounded public HTML response for the shell contract only."""

    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit public URL input
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(response.getcode())
            content_type = str(response.headers.get("content-type") or "")
            if len(body) > MAX_RESPONSE_BYTES:
                return {
                    "status_code": status,
                    "content_type": content_type,
                    "error_code": "response_too_large",
                }
            return {
                "status_code": status,
                "content_type": content_type,
                "body": body.decode("utf-8", errors="replace"),
            }
    except HTTPError as exc:
        return {
            "status_code": int(exc.code),
            "content_type": str(exc.headers.get("content-type") or "") if exc.headers else "",
            "error_code": f"http_{exc.code}",
        }
    except (TimeoutError, URLError, OSError) as exc:
        return {"status_code": None, "content_type": "", "error_code": "network_error", "error": str(exc)}


def _count_payload(payload: Any, keys: Sequence[str]) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _summarize_route(route: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    payload = response.get("payload")
    summary: dict[str, Any] = {
        "name": route["name"],
        "path": route["path"],
        "status_code": response.get("status_code"),
        "content_type": response.get("content_type"),
        "error_code": response.get("error_code"),
    }
    if isinstance(payload, Mapping):
        summary["payload_status"] = payload.get("status")
        count = _count_payload(payload, route.get("collection_keys", ()))
        if count is not None:
            summary["item_count"] = count
        elif route["name"] == "ledger":
            summary["ledger_counts"] = {
                key: value
                for key, value in payload.items()
                if key in {"fixtures", "observations", "predictions", "intelligence", "lineups", "odds", "news", "weather", "stages", "aliases", "sourceRuns"}
                and isinstance(value, int)
                and not isinstance(value, bool)
            }
        elif route["name"] == "evaluations":
            summary["has_overall"] = isinstance(payload.get("overall"), Mapping)
    elif isinstance(payload, list):
        summary["item_count"] = len(payload)
    return summary


def _summarize_html_route(route: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimum server-rendered HTML contract without scraping facts."""

    body = response.get("body")
    content_type = str(response.get("content_type") or "")
    required = tuple(str(item) for item in route.get("required_markers", ()))
    forbidden = tuple(str(item) for item in route.get("forbidden_markers", ()))
    errors: list[str] = []
    if response.get("status_code") != 200:
        errors.append("http_status_not_200")
    if "html" not in content_type.lower():
        errors.append("content_type_not_html")
    if not isinstance(body, str) or not body.strip():
        errors.append("html_body_missing")
        body = ""
    missing = [marker for marker in required if marker not in body]
    if missing:
        errors.append("required_marker_missing")
    present_forbidden = [marker for marker in forbidden if marker in body]
    if present_forbidden:
        errors.append("forbidden_marker_present")
    return {
        "name": route["name"],
        "path": route["path"],
        "kind": "html",
        "status_code": response.get("status_code"),
        "content_type": content_type,
        "error_code": response.get("error_code"),
        "body_bytes": len(body.encode("utf-8")) if isinstance(body, str) else 0,
        "contract_ok": not errors,
        "contract_errors": errors,
        "missing_markers": missing[:10],
        "forbidden_markers": present_forbidden[:10],
    }


def probe_public_site(
    base_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Callable[[str], Mapping[str, Any]] = _bounded_json_response,
    html_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Probe expected public API and HTML routes and classify readiness.

    The default network probe runs both route classes.  A custom JSON fetcher
    keeps the historical unit-test contract; callers that want HTML coverage
    with a custom transport must pass ``html_fetcher`` explicitly.
    """

    origin = _base_url(base_url)
    routes: list[dict[str, Any]] = []
    payloads: dict[str, Any] = {}
    for route in ROUTES:
        url = urljoin(origin, str(route["path"]))
        try:
            response = fetcher(url, timeout=timeout)
        except TypeError:
            response = fetcher(url)
        if not isinstance(response, Mapping):
            response = {"status_code": None, "content_type": "", "error_code": "invalid_probe_response"}
        payloads[str(route["name"])] = response.get("payload")
        routes.append(_summarize_route(route, response))

    html_routes: list[dict[str, Any]] = []
    html_checks_skipped = False
    if html_fetcher is None:
        if fetcher is _bounded_json_response:
            html_fetcher = _bounded_html_response
        else:
            html_checks_skipped = True
    if html_fetcher is not None:
        for route in HTML_ROUTES:
            url = urljoin(origin, str(route["path"]))
            try:
                response = html_fetcher(url, timeout=timeout)
            except TypeError:
                response = html_fetcher(url)
            if not isinstance(response, Mapping):
                response = {"status_code": None, "content_type": "", "error_code": "invalid_probe_response"}
            html_routes.append(_summarize_html_route(route, response))
    routes.extend(html_routes)

    http_successes = [route for route in routes if route.get("status_code") == 200]
    route_failures = [
        route
        for route in routes
        if route.get("status_code") != 200 or route.get("contract_ok") is False
    ]
    data_counts = {
        "upcoming_matches": next((r.get("item_count", 0) for r in routes if r["name"] == "upcoming_matches"), 0),
        "live_matches": next((r.get("item_count", 0) for r in routes if r["name"] == "live_matches"), 0),
        "source_runs": next((r.get("item_count", 0) for r in routes if r["name"] == "source_runs"), 0),
    }
    ledger_counts = next((r.get("ledger_counts", {}) for r in routes if r["name"] == "ledger"), {})
    has_ledger_rows = any(value > 0 for value in ledger_counts.values() if isinstance(value, int) and not isinstance(value, bool))
    has_runtime_rows = any(value > 0 for value in data_counts.values()) or has_ledger_rows
    if not http_successes:
        status = "unreachable"
    elif route_failures:
        status = "stale_or_incomplete_routes"
    elif not has_runtime_rows:
        status = "reachable_empty_runtime"
    else:
        status = "ready_for_data_review"
    prospective_payload = payloads.get("prospective_status")
    prospective_gate = prospective_payload.get("gate") if isinstance(prospective_payload, Mapping) else None
    gate_available = isinstance(prospective_gate, Mapping)
    production_allowed = bool(
        gate_available
        and prospective_payload.get("productionReady") is True
        and prospective_gate.get("productionAllowed") is True
    )
    deployment_ready = status == "ready_for_data_review"
    return {
        "schema_version": "1.0.0",
        "base_url": origin.rstrip("/"),
        "status": status,
        "deployment_ready": deployment_ready,
        "production_ready": deployment_ready and production_allowed,
        "production_gate": {
            "available": gate_available,
            "status": prospective_gate.get("status") if gate_available else None,
            "production_allowed": production_allowed,
            "sample_requirements_met": prospective_gate.get("sampleRequirementsMet") if gate_available else None,
            "all_required_targets_scored": prospective_gate.get("allRequiredTargetsScored") if gate_available else None,
            "prediction_freezes_verified": prospective_gate.get("predictionFreezesVerified") if gate_available else None,
        },
        "reachable": bool(http_successes),
        "all_expected_routes_ok": not route_failures,
        "html_checks_skipped": html_checks_skipped,
        "html_routes": html_routes,
        "has_runtime_rows": has_runtime_rows,
        "data_counts": data_counts,
        "ledger_counts": ledger_counts,
        "routes": routes,
        "policy": "read_only_public_api_probe; no local fallback, credential, or remote write",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("MATCHLINE_PUBLIC_URL", ""))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = probe_public_site(args.base_url, timeout=args.timeout)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["production_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["probe_public_site"]
