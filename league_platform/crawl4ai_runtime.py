"""Operational diagnostics for the optional Crawl4AI edge adapter.

The production fetcher intentionally treats Crawl4AI as an opt-in browser
edge.  This module gives an operator a read-only answer to two separate
questions that otherwise get conflated in source-health dashboards:

* is the optional ``crawl4ai`` package available in this Python environment;
* has an explicit, allowlisted source configuration been supplied.

It never discovers URLs, starts a browser, reads credentials, or changes the
source configuration.  A missing package and an empty configuration are both
valid non-production states, but they must remain distinguishable in audit
evidence.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from league_platform.live_sources.crawl4ai import (
    Crawl4AIConfig,
    load_crawl4ai_configs,
    strict_tls_runtime_status,
)
from league_platform.source_registry import get_source_registry


def _dependency_status() -> dict[str, Any]:
    """Return package availability without importing Crawl4AI or launching Chromium."""

    spec = importlib.util.find_spec("crawl4ai")
    if spec is None:
        return {"status": "missing", "version": None}
    try:
        package_version = importlib.metadata.version("crawl4ai")
    except importlib.metadata.PackageNotFoundError:
        package_version = None
    return {"status": "installed", "version": package_version}


def _browser_status() -> dict[str, Any]:
    """Report a usable browser channel without launching a browser.

    Crawl4AI can use a Playwright-managed Chromium build or an installed
    Chrome channel.  Checking both paths here prevents a configured source
    from being reported as ready when the optional package is installed but
    no executable exists.  This is read-only and deliberately does not run
    ``crawl4ai-setup`` or download browser binaries.
    """

    configured = os.environ.get("MATCHLINE_CRAWL4AI_CHROME", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        resolved = shutil.which(candidate)
        if resolved:
            return {"status": "available", "mode": "chrome_channel", "executable": resolved}

    cache_root = Path.home() / ".cache" / "ms-playwright"
    if cache_root.is_dir():
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome", "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"):
            matches = sorted(cache_root.glob(pattern))
            if matches:
                return {"status": "available", "mode": "playwright_cache", "executable": str(matches[0])}
    return {"status": "missing", "mode": None, "executable": None}


def _config_status(
    configs: Sequence[Crawl4AIConfig],
    errors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rights_blocked_sources = [
        {
            "source_id": error.get("source_id"),
            "name": error.get("name"),
            "url": error.get("url"),
            "license_status": error.get("license_status"),
            "rights_reference": error.get("rights_reference"),
            "network_opened": False,
        }
        for error in errors
        if error.get("status") == "rights_blocked"
    ]
    if errors and rights_blocked_sources and len(rights_blocked_sources) == len(errors):
        status = "configured_with_rights_blocks" if configs else "rights_blocked"
    elif errors:
        status = "invalid_config"
    elif configs:
        status = "configured"
    else:
        status = "not_configured"
    registry_rows = {str(row.get("id")): row for row in get_source_registry() if row.get("id")}

    def registry_row(source_id: str | None) -> Mapping[str, Any] | None:
        if not source_id:
            return None
        row = registry_rows.get(source_id)
        return row if isinstance(row, Mapping) else None

    def identity_status(source_id: str | None) -> str:
        if not source_id:
            return "missing_display_only_identity"
        row = registry_row(source_id)
        if row is None:
            return "unregistered_source_id"
        if row.get("role", "fact_source") == "fetch_runtime" or row.get("fact_source") is False:
            return "fetch_runtime_id_not_fact_source"
        return "registered_fact_source"

    return {
        "status": status,
        "configured_count": len(configs),
        "rights_blocked_count": len(rights_blocked_sources),
        "rights_blocked_sources": rights_blocked_sources,
        "sources": [
            {
                "name": config.name,
                "source_id": config.source_id,
                "parser_contract": config.parser,
                "capture_engine": "Crawl4AI",
                "execution_layer": "Crawl4AI",
                "host": config.allowed_hosts[0] if config.allowed_hosts else None,
                "url": config.url,
                "enabled": config.enabled,
                "license_status": config.license_status,
                "fact_source": identity_status(config.source_id) == "registered_fact_source",
                "registry_status": (registry_row(config.source_id) or {}).get("status"),
                "registry_source_tier": (registry_row(config.source_id) or {}).get("source_tier"),
                "model_eligible_requested": config.model_eligible,
                "follow_links": config.follow_links,
                "max_follow_up_pages": config.max_follow_up_pages,
                "follow_record_fields": list(config.follow_record_fields),
                "follow_parser": config.follow_parser,
                "follow_up_min_delay_seconds": config.follow_up_min_delay_seconds,
                "source_identity_status": identity_status(config.source_id),
            }
            for config in configs
        ],
        "errors": [dict(error) for error in errors[:20]],
    }


def _registry_audit(configs: Sequence[Crawl4AIConfig]) -> dict[str, Any]:
    """Audit Crawl4AI declarations against the versioned source registry.

    Crawl4AI is a shared browser execution layer.  This audit makes the
    distinction machine-readable before a browser is started: only a
    registered fact-source ID may be projected as a source; the execution
    layer's own ID and unknown IDs are never valid fact-source identities.
    Anonymous legacy configs remain crawlable for display evidence, but are
    explicitly marked as unidentified and cannot enter the model.
    """

    rows = {str(row.get("id")): row for row in get_source_registry() if row.get("id")}
    configured_ids = sorted({config.source_id for config in configs if config.source_id})
    anonymous_count = sum(1 for config in configs if not config.source_id)
    unregistered_ids = sorted(source_id for source_id in configured_ids if source_id not in rows)
    runtime_ids = sorted(
        source_id
        for source_id in configured_ids
        if source_id in rows
        and (rows[source_id].get("role", "fact_source") == "fetch_runtime" or rows[source_id].get("fact_source") is False)
    )
    requested_model_without_fact_source = sorted(
        config.source_id or "<anonymous>"
        for config in configs
        if config.model_eligible
        and (
            not config.source_id
            or config.source_id in unregistered_ids
            or config.source_id in runtime_ids
        )
    )
    invalid = bool(unregistered_ids or runtime_ids or requested_model_without_fact_source)
    status = "invalid" if invalid else "ok" if not anonymous_count else "warning_anonymous_identity"
    # Keep the fan-out contract explicit at the runtime boundary.  A single
    # Crawl4AI process can serve many registered fact sources, but the process
    # itself is never one of those sources.  The longer
    # ``registered_fact_source_count`` field is retained for compatibility;
    # the shorter counters are intended for dashboards and operators.
    registered_fact_source_count = sum(
        1
        for source_id in configured_ids
        if source_id in rows and source_id not in runtime_ids
    )

    # A Crawl4AI process is an execution layer that can carry many independent
    # fact-source lanes.  Keep the inventory scoped to rows whose declared
    # adapter is Crawl4AI; direct HTTP adapters are not silently counted as
    # Crawl4AI coverage, and blocked/forbidden rows remain visible as policy
    # state rather than looking like missing configuration.
    crawl4ai_fact_rows = {
        source_id: row
        for source_id, row in rows.items()
        if row.get("adapter") == "league_platform.live_sources.crawl4ai"
        and row.get("role", "fact_source") != "fetch_runtime"
        and row.get("fact_source") is not False
    }
    configured_crawl4ai_fact_source_ids = sorted(
        source_id for source_id in configured_ids if source_id in crawl4ai_fact_rows
    )
    unconfigured_crawl4ai_fact_source_ids = sorted(
        source_id for source_id in crawl4ai_fact_rows if source_id not in configured_ids
    )
    blocked_crawl4ai_fact_source_ids = sorted(
        source_id
        for source_id, row in crawl4ai_fact_rows.items()
        if str(row.get("status") or "") in {"blocked_by_robots", "forbidden"}
    )
    return {
        "status": status,
        "capture_engine": "Crawl4AI",
        "role": "fetch_runtime",
        "fact_source": False,
        "fanout_rule": "one_runtime_to_many_registered_fact_source_ids",
        "execution_layer_count": 1,
        "fact_source_count": registered_fact_source_count,
        "fanout_source_count": len(configured_ids),
        "configured_source_ids": configured_ids,
        "declared_source_count": len(configured_ids),
        "anonymous_config_count": anonymous_count,
        "unregistered_source_ids": unregistered_ids,
        "fetch_runtime_ids_used_as_fact_source": runtime_ids,
        "model_eligible_without_registered_fact_source": requested_model_without_fact_source,
        "registered_fact_source_count": registered_fact_source_count,
        "registered_crawl4ai_fact_source_count": len(crawl4ai_fact_rows),
        "registered_crawl4ai_fact_source_ids": sorted(crawl4ai_fact_rows),
        "configured_crawl4ai_fact_source_ids": configured_crawl4ai_fact_source_ids,
        "unconfigured_crawl4ai_fact_source_count": len(unconfigured_crawl4ai_fact_source_ids),
        "unconfigured_crawl4ai_fact_source_ids": unconfigured_crawl4ai_fact_source_ids,
        "blocked_crawl4ai_fact_source_ids": blocked_crawl4ai_fact_source_ids,
    }


def inspect_crawl4ai_runtime(
    *,
    raw: str | bytes | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build a serialisable, read-only Crawl4AI readiness report."""

    configs, errors = load_crawl4ai_configs(raw=raw, config_path=config_path)
    dependency = _dependency_status()
    browser = _browser_status()
    tls_runtime = strict_tls_runtime_status() if dependency["status"] == "installed" else {"status": "missing", "reason": "Crawl4AI package is not installed"}
    configuration = _config_status(configs, errors)
    registry_audit = _registry_audit(configs)
    configured_statuses = {"configured", "configured_with_rights_blocks"}
    if configuration["status"] in configured_statuses and registry_audit["status"] == "invalid":
        readiness = "blocked_source_registry"
    elif configuration["status"] in configured_statuses and dependency["status"] == "missing":
        readiness = "blocked_dependency_missing"
    elif configuration["status"] == "rights_blocked":
        readiness = "blocked_rights"
    elif configuration["status"] == "invalid_config":
        readiness = "blocked_invalid_config"
    elif configuration["status"] == "not_configured":
        readiness = "not_configured"
    elif tls_runtime["status"] != "supported":
        readiness = "blocked_tls_policy_unsupported"
    elif browser["status"] == "missing":
        readiness = "blocked_browser_missing"
    elif configuration["status"] == "configured_with_rights_blocks":
        # A partial rights block is not a ready runtime: claiming readiness
        # would make the blocked lanes look executable to operators.  Keep a
        # distinct partial state so a separately admissible lane remains
        # visible without greenwashing the central policy failures.
        readiness = "blocked_rights_partial"
    else:
        readiness = "ready_for_explicit_crawl"
    return {
        "schema_version": "1.0.0",
        "adapter": "league_platform.live_sources.crawl4ai",
        "dependency": dependency,
        "browser": browser,
        "tls_runtime": tls_runtime,
        "configuration": configuration,
        "source_registry_audit": registry_audit,
        "readiness": readiness,
        "browser_started": False,
        "model_use": "display_only_unstructured_until_source_parser",
        "security_policy": {
            "ignore_https_errors": False,
            "robots_check": "required",
        "redirects": "same_host_and_allowlisted_path_only",
            "login_captcha_bypass": False,
            "access_control_bypass": False,
        },
        "policy": "No URL discovery, login, captcha, robots or access-control bypass is performed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="explicit Crawl4AI JSON configuration")
    parser.add_argument("--output", type=Path, help="optional JSON evidence output path")
    args = parser.parse_args()
    report = inspect_crawl4ai_runtime(config_path=args.config)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


__all__ = ["inspect_crawl4ai_runtime"]


if __name__ == "__main__":
    main()
