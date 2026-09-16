"""Bounded Crawl4AI adapter for public, browser-rendered football sources.

The browser is intentionally kept at the edge of the pipeline.  A crawl is
stored as append-only evidence and exposed to the display layer, but an
unstructured page never becomes a model feature by itself.  Source-specific
parsers stay narrow and source-owned: current adapters only project explicitly
declared WhoScored, Premier League, LaLiga, Bundesliga, or Serie A match fields
and declared first-party news cards/JSON-LD articles into display/audit records.  They still
must pass the normal provenance, entity-join, license, and time-cutoff gates
before any separate model adapter could consider them.

The optional :mod:`crawl4ai` dependency is imported lazily.  This keeps the
normal HTTP-only refresh usable on machines that do not have a Chromium
runtime, while a production worker can install Crawl4AI and enable only the
explicitly allowlisted URLs from ``MATCHLINE_CRAWL4AI_SOURCES``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
from html.parser import HTMLParser
import inspect
import json
import math
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urljoin, urlparse

from league_platform import source_rights


DEFAULT_ARCHIVE_DIR = Path("data/live/crawl4ai")
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_MARKDOWN_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 45.0
RUNNER_TIMEOUT_MAX_GRACE_SECONDS = 15.0
DEFAULT_DELAY_SECONDS = 30.0
DEFAULT_MAX_HOST_WORKERS = 4
MAX_HOST_WORKERS = 8
DEFAULT_USER_AGENT = "MatchlineResearch/1.0 (+https://predict.hetaisheng.ccwu.cc)"
ROBOTS_MAX_BYTES = 128 * 1024
MAX_JSON_LD_SCRIPTS = 24
MAX_JSON_LD_BYTES = 256 * 1024
MAX_EXTRACTED_RECORDS = 50
MAX_FOLLOW_UP_PAGES = 20
FOLLOW_RECORD_FIELDS = frozenset({"match_url", "article_url"})
THROTTLE_STATE_FILENAME = "host_throttle.json"
LATEST_PAGES_FILENAME = "latest_pages.json"
LATEST_PAGES_LOCK_FILENAME = "latest_pages.json.lock"
PUBLIC_DISPLAY_TIMEZONE = "Europe/London"
PREMIER_LEAGUE_MATCH_CARD_SELECTOR = "css:[data-testid='matchCard']"
PARSER_CONTRACTS = frozenset(
    {
        "auto",
        "football_data_archive_v1",
        "generic_public_page_v1",
        "official_news_cards_v1",
        "official_news_jsonld_v1",
        "seriea_public_fixtures_v1",
        "sports_event_jsonld_v1",
        "whoscored_public_fixtures_v1",
        "premier_league_public_fixtures_v1",
        "laliga_public_sports_events_v1",
    }
)
TLS_BYPASS_FLAGS = frozenset(
    {
        "--ignore-certificate-errors",
        "--ignore-certificate-errors-spki-list",
    }
)

# Crawl4AI is a shared execution layer.  Keep this identity explicit in every
# aggregate response so downstream consumers cannot mistake the browser
# runtime for one of the declared football fact sources.
CRAWL4AI_EXECUTION_LAYER_ID = "crawl4ai_allowlisted_pages"
CRAWL4AI_EXECUTION_LAYER_NAME = "Crawl4AI"

# A Crawl4AI page is admissible only when its parser contract belongs to the
# declared factual source.  ``auto`` is intentionally handled as a host
# routing alias below; it is never a way to let one provider's parser stand in
# for another provider.  Follow-up detail pages use the additional contracts
# listed for the parent source.
SOURCE_PARSER_CONTRACTS: dict[str, frozenset[str]] = {
    "football_data_historical": frozenset({"football_data_archive_v1"}),
    "fbref_public_stats": frozenset({"generic_public_page_v1"}),
    "whoscored_public_pages": frozenset({"auto", "whoscored_public_fixtures_v1"}),
    "premier_league_public_pages": frozenset(
        {"auto", "premier_league_public_fixtures_v1", "sports_event_jsonld_v1"}
    ),
    "laliga_public_pages": frozenset(
        {"auto", "laliga_public_sports_events_v1", "sports_event_jsonld_v1"}
    ),
    "laliga_official_news": frozenset({"auto", "official_news_jsonld_v1"}),
    "bundesliga_public_pages": frozenset({"auto", "sports_event_jsonld_v1"}),
    "seriea_public_pages": frozenset(
        {"auto", "seriea_public_fixtures_v1", "sports_event_jsonld_v1"}
    ),
    "ligue1_official_news": frozenset({"auto", "official_news_cards_v1"}),
    "clubelo_public_ratings": frozenset({"generic_public_page_v1"}),
    "sofifa_public_reference": frozenset({"generic_public_page_v1"}),
}

# Registry hosts are part of the parser/source identity contract.  ``auto``
# is a backwards-compatible routing alias, but it must not be able to route a
# declared source through another provider's host/parser.  Keep this bounded
# table local to the execution edge instead of trusting operator-supplied
# ``allowed_hosts`` (which is an input boundary, not source identity proof).
SOURCE_IDENTITY_HOSTS: dict[str, frozenset[str]] = {
    "football_data_historical": frozenset({"www.football-data.co.uk", "football-data.co.uk"}),
    "fbref_public_stats": frozenset({"www.fbref.com", "fbref.com"}),
    "whoscored_public_pages": frozenset({"www.whoscored.com", "whoscored.com"}),
    "premier_league_public_pages": frozenset({"www.premierleague.com", "premierleague.com"}),
    "laliga_public_pages": frozenset({"www.laliga.com", "laliga.com"}),
    "laliga_official_news": frozenset({"www.laliga.com", "laliga.com"}),
    "bundesliga_public_pages": frozenset({"www.bundesliga.com", "bundesliga.com"}),
    "seriea_public_pages": frozenset({"en.legaseriea.it", "www.legaseriea.it"}),
    "ligue1_official_news": frozenset({"www.ligue1.com", "ligue1.com"}),
    "clubelo_public_ratings": frozenset({"www.clubelo.com", "clubelo.com"}),
    "sofifa_public_reference": frozenset({"www.sofifa.com", "sofifa.com"}),
}

# ``SourceId`` is the authoritative closed policy inventory.  IDs that are
# known there but do not have a Crawl4AI parser contract (for example
# OpenFootball or OpenLigaDB) must not be attached to this browser edge even
# when a test seam supplies an ALLOW decision.
_KNOWN_POLICY_SOURCE_IDS = frozenset(item.value for item in source_rights.SourceId)


def _execution_layer_metadata(
    configs: Sequence["Crawl4AIConfig"],
    pages: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return bounded fan-out lineage for the shared Crawl4AI edge.

    ``source_id`` is the only identity that counts as a fact source.  A page
    without it remains an unidentified execution result and is deliberately
    excluded from the declared-source count.  This summary is operational
    metadata; it never grants model eligibility to any page.
    """

    configured: dict[str, dict[str, Any]] = {}
    for config in configs:
        source_id = str(config.source_id or "").strip()
        if not source_id:
            continue
        configured.setdefault(
            source_id,
            {
                "source_id": source_id,
                "source_name": config.name,
                "configured_page_count": 0,
                "page_count": 0,
                "fresh_page_count": 0,
                "stale_page_count": 0,
                "extracted_record_count": 0,
                "error_count": 0,
                "status": "not_observed",
            },
        )["configured_page_count"] += 1

    unidentified_page_count = 0
    unidentified_error_count = 0
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        source_id = str(page.get("source_id") or page.get("fact_source_id") or "").strip()
        row = configured.get(source_id)
        if row is None:
            unidentified_page_count += 1
            continue
        row["page_count"] += 1
        if page.get("stale") is True:
            row["stale_page_count"] += 1
        else:
            row["fresh_page_count"] += 1
        records = page.get("extracted_records")
        if isinstance(records, list):
            row["extracted_record_count"] += len(records)

    for error in errors:
        if not isinstance(error, Mapping):
            continue
        source_id = str(error.get("source_id") or error.get("fact_source_id") or "").strip()
        row = configured.get(source_id)
        if row is None:
            unidentified_error_count += 1
            continue
        row["error_count"] += 1

    for row in configured.values():
        if row["fresh_page_count"]:
            row["status"] = "fresh"
        elif row["stale_page_count"]:
            row["status"] = "stale"
        elif row["error_count"]:
            row["status"] = "error"
        row["model_eligible_count"] = 0
        row["enters_model"] = False

    return {
        "execution_layer": {
            "id": CRAWL4AI_EXECUTION_LAYER_ID,
            "name": CRAWL4AI_EXECUTION_LAYER_NAME,
            "role": "fetch_runtime",
            "fact_source": False,
            "model_eligible": False,
        },
        "fact_source_ids": list(configured),
        "fact_source_count": len(configured),
        "source_fanout": list(configured.values()),
        "unidentified_page_count": unidentified_page_count,
        "unidentified_error_count": unidentified_error_count,
    }


def _is_tls_bypass_flag(value: Any) -> bool:
    """Return whether one Chromium argument weakens certificate validation."""

    flag = str(value).strip()
    return flag in TLS_BYPASS_FLAGS or flag.startswith("--ignore-certificate-errors-spki-list")


def _filter_tls_bypass_flags(values: Sequence[Any]) -> list[str]:
    """Remove only certificate-bypass flags from a third-party argument list.

    This is intentionally a deny-list filter, not a general launch-argument
    rewrite.  All other Crawl4AI/Playwright arguments are preserved exactly.
    """

    return [str(value) for value in values if not _is_tls_bypass_flag(value)]


def _crawl4ai_tls_launch_inventory() -> dict[str, Any]:
    """Inspect raw and effective launch flags without starting Chromium.

    Crawl4AI 0.9.x hard-codes certificate-bypass flags in both its managed
    and native browser paths.  The local compatibility layer removes only
    those flags before launch and uses a Playwright context with
    ``ignore_https_errors=False``.  This inventory proves the effective
    argument set rather than trusting the SDK's config field alone.
    """

    from crawl4ai import BrowserConfig
    from crawl4ai.browser_manager import BrowserManager, ManagedBrowser

    config = BrowserConfig(ignore_https_errors=False, use_persistent_context=False)
    managed_flags = list(ManagedBrowser.build_browser_flags(config))
    manager_args = BrowserManager(config)._build_browser_args()
    if not isinstance(manager_args, Mapping):
        raise RuntimeError("Crawl4AI browser manager returned a non-mapping launch config")
    raw_args = manager_args.get("args", ())
    if not isinstance(raw_args, Sequence) or isinstance(raw_args, (str, bytes, bytearray)):
        raise RuntimeError("Crawl4AI browser manager returned invalid launch args")
    raw_flags = [*managed_flags, *list(raw_args)]
    raw_unsafe = sorted({flag for flag in raw_flags if _is_tls_bypass_flag(flag)})
    effective_flags = _filter_tls_bypass_flags(raw_flags)
    effective_unsafe = sorted({flag for flag in effective_flags if _is_tls_bypass_flag(flag)})
    return {
        "raw_flags": raw_flags,
        "raw_unsafe_flags": raw_unsafe,
        "effective_flags": effective_flags,
        "effective_unsafe_flags": effective_unsafe,
        "compatibility_patch": "strict_tls_flag_filter_v1",
        "browser_mode": "native_context",
        "ignore_https_errors": bool(getattr(config, "ignore_https_errors", True)),
    }


@contextmanager
def _strict_tls_compat_patch() -> Iterator[None]:
    """Temporarily make Crawl4AI's two browser paths TLS-strict.

    The patch is process-local and restored immediately after the crawl.  It
    only filters the two certificate-bypass arguments; Playwright still gets
    ``ignore_https_errors=False`` through the BrowserConfig context.  If a
    future Crawl4AI version changes either method shape, the caller fails
    closed instead of launching an unverified browser.
    """

    from crawl4ai.browser_manager import BrowserManager, ManagedBrowser

    original_managed_descriptor = ManagedBrowser.__dict__.get("build_browser_flags")
    original_managed = ManagedBrowser.build_browser_flags
    original_manager = BrowserManager._build_browser_args
    original_manager_descriptor = BrowserManager.__dict__.get("_build_browser_args")
    if (
        original_managed_descriptor is None
        or original_manager_descriptor is None
        or not callable(original_managed)
        or not callable(original_manager)
    ):
        raise RuntimeError("Crawl4AI browser launch methods are not patchable")

    def strict_managed_flags(config: Any) -> list[str]:
        return _filter_tls_bypass_flags(original_managed(config))

    def strict_manager_args(manager: Any) -> dict[str, Any]:
        result = original_manager(manager)
        if not isinstance(result, Mapping):
            raise RuntimeError("Crawl4AI browser manager returned a non-mapping launch config")
        values = result.get("args", ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise RuntimeError("Crawl4AI browser manager returned invalid launch args")
        patched = dict(result)
        patched["args"] = _filter_tls_bypass_flags(values)
        return patched

    ManagedBrowser.build_browser_flags = staticmethod(strict_managed_flags)
    BrowserManager._build_browser_args = strict_manager_args
    try:
        yield
    finally:
        ManagedBrowser.build_browser_flags = original_managed_descriptor
        BrowserManager._build_browser_args = original_manager_descriptor


def _security_policy() -> dict[str, Any]:
    """Return the non-negotiable browser/source boundary for one run.

    Keep this in the returned snapshot rather than only in operator docs so a
    later Sites reader can distinguish a strict failure from a browser that
    was configured to weaken TLS or access controls.
    """

    return {
        "ignore_https_errors": False,
        "robots_check": "required",
        "redirects": "same_host_and_allowlisted_path_only",
        "login_captcha_bypass": False,
        "access_control_bypass": False,
    }


def strict_tls_runtime_status() -> dict[str, Any]:
    """Inspect Crawl4AI launch arguments without starting a browser.

    Crawl4AI 0.9.x exposes ``ignore_https_errors`` on its config but also
    hard-codes Chromium certificate-bypass flags in its browser manager.  A
    config value alone therefore does not prove strict TLS.  The local
    compatibility layer strips only those flags before launch and keeps the
    context-level setting false; an uninspectable effective launch is still
    blocked.
    """

    try:
        inventory = _crawl4ai_tls_launch_inventory()
    except ModuleNotFoundError as exc:
        return {"status": "missing", "reason": f"Crawl4AI TLS runtime is unavailable: {exc}"}
    except Exception as exc:  # version drift must fail closed
        return {"status": "unverified", "reason": f"Crawl4AI TLS launch arguments could not be verified: {exc}"}

    if inventory["ignore_https_errors"] is not False:
        return {
            "status": "unsupported",
            "reason": "Crawl4AI context does not prove ignore_https_errors=false",
            "unsafe_flags": inventory["raw_unsafe_flags"],
            "effective_unsafe_flags": inventory["effective_unsafe_flags"],
            "compatibility_patch": inventory["compatibility_patch"],
        }
    if inventory["effective_unsafe_flags"]:
        return {
            "status": "unsupported",
            "reason": "effective Crawl4AI launch arguments still contain certificate-bypass flags",
            "unsafe_flags": inventory["raw_unsafe_flags"],
            "effective_unsafe_flags": inventory["effective_unsafe_flags"],
            "compatibility_patch": inventory["compatibility_patch"],
        }
    raw_unsafe = inventory["raw_unsafe_flags"]
    if raw_unsafe:
        reason = "strict adapter removes Crawl4AI's hard-coded certificate-bypass flags before launch"
    else:
        reason = "Crawl4AI launch arguments contain no certificate-bypass flags"
    return {
        "status": "supported",
        "reason": reason,
        "unsafe_flags": raw_unsafe,
        "effective_unsafe_flags": [],
        "compatibility_patch": inventory["compatibility_patch"],
        "browser_mode": inventory["browser_mode"],
        "ignore_https_errors": False,
    }


class Crawl4AIConfigError(ValueError):
    """Raised when a Crawl4AI source configuration is unsafe or incomplete."""


class _Crawl4AIRightsBlocked(Crawl4AIConfigError):
    """Internal marker for a syntactically valid source blocked by rights."""


class _RunnerDeadlineExceeded(Exception):
    """Internal sentinel for the adapter's own wall-clock deadline."""


@dataclass(frozen=True, slots=True)
class Crawl4AIConfig:
    """One explicitly allowlisted public page source.

    ``model_eligible`` is retained as an audit/configuration field, but the
    page adapter always emits ``enters_model=False``.  A structured,
    source-specific parser must make a separate decision after exact fixture
    joins and publication-time checks.
    """

    name: str
    url: str
    # Human-readable ``name`` and Crawl4AI's runtime identity are not the
    # factual provider.  An operator may declare the provider's stable
    # registry ID explicitly; legacy configs without it stay display-only and
    # are treated as unidentified by ingestion.
    source_id: str | None = None
    # The fetch runtime is shared, but the parser contract belongs to the
    # declared fact source. ``auto`` preserves the legacy host-based routing;
    # explicit contracts are required when one source exposes pages on a host
    # that is shared with another product or when a new source is onboarded.
    parser: str = "auto"
    enabled: bool = True
    # A custom runner is a fixture seam for unit/integration tests only.  It
    # must be explicitly opted into on every config and its output is marked
    # non-runtime evidence by ``fetch_crawl4ai``.
    test_only: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_path_prefixes: tuple[str, ...] = ("/",)
    source_tier: str = "reliable_public_provider"
    model_eligible: bool = False
    license_status: str = "unknown"
    # Auditable public-license page or written-authorization reference.  A
    # bare operator assertion is not enough to start a commercial crawler.
    rights_reference: str | None = None
    check_robots: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_bytes: int = DEFAULT_MAX_BYTES
    min_delay_seconds: float = DEFAULT_DELAY_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    # Link expansion is opt-in and source-declared.  It only follows URLs
    # already projected by the source parser; Crawl4AI never discovers or
    # crawls arbitrary page links.
    follow_links: bool = False
    max_follow_up_pages: int = 0
    follow_record_fields: tuple[str, ...] = ("match_url", "article_url")
    follow_parser: str | None = None
    follow_up_min_delay_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise Crawl4AIConfigError("source name is required")
        if self.source_id is not None:
            if not isinstance(self.source_id, str) or not self.source_id.strip():
                raise Crawl4AIConfigError("source_id must be a non-empty string when provided")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,99}", self.source_id.strip()):
                raise Crawl4AIConfigError("source_id must use 2-100 lowercase letters, digits, '_' or '-'")
            object.__setattr__(self, "source_id", self.source_id.strip())
        if not isinstance(self.parser, str) or self.parser not in PARSER_CONTRACTS:
            raise Crawl4AIConfigError(
                "parser must be one of: " + ", ".join(sorted(PARSER_CONTRACTS))
            )
        if not isinstance(self.enabled, bool):
            raise Crawl4AIConfigError("enabled must be a boolean")
        if not isinstance(self.test_only, bool):
            raise Crawl4AIConfigError("test_only must be a boolean")
        parsed = _validate_url(self.url)
        hosts = tuple(str(item).strip().lower().rstrip(".") for item in self.allowed_hosts if str(item).strip())
        if not hosts:
            hosts = (str(parsed.hostname).lower().rstrip("."),)
        object.__setattr__(self, "allowed_hosts", hosts)
        prefixes = tuple(str(item) for item in self.allowed_path_prefixes if str(item))
        if not prefixes:
            prefixes = ("/",)
        if any(not item.startswith("/") for item in prefixes):
            raise Crawl4AIConfigError("allowed_path_prefixes must start with '/'")
        object.__setattr__(self, "allowed_path_prefixes", prefixes)
        if not _host_allowed(str(parsed.hostname), hosts):
            raise Crawl4AIConfigError(f"URL host is outside the allowlist: {parsed.hostname}")
        if not any((parsed.path or "/").startswith(prefix) for prefix in prefixes):
            raise Crawl4AIConfigError(f"URL path is outside the allowlist: {parsed.path or '/'}")
        if self.source_tier not in {
            "official",
            "authorized",
            "reliable_public_provider",
            "reliable_media",
            "social",
        }:
            raise Crawl4AIConfigError(f"unknown source tier: {self.source_tier}")
        if self.license_status not in {"unknown", "open", "authorized", "restricted", "denied"}:
            raise Crawl4AIConfigError("license_status must be unknown, open, authorized, restricted, or denied")
        if self.rights_reference is not None:
            if not isinstance(self.rights_reference, str) or not self.rights_reference.strip():
                raise Crawl4AIConfigError("rights_reference must be a non-empty string")
            object.__setattr__(self, "rights_reference", self.rights_reference.strip())
        # ``license_status`` and ``rights_reference`` are operator declarations,
        # not an authority boundary.  Keep their shape validation above, but do
        # not reject an enabled row here: the central v260 source-rights policy
        # must be consulted first and its structured decision is what callers
        # should observe for unknown/restricted/denied rows.
        if isinstance(self.check_robots, bool) is False:
            raise Crawl4AIConfigError("check_robots must be a boolean")
        _finite_range(self.timeout_seconds, minimum=0.1, maximum=120.0, field="timeout_seconds")
        _integer_range(self.max_bytes, minimum=1, maximum=16 * 1024 * 1024, field="max_bytes")
        _finite_range(self.min_delay_seconds, minimum=0.0, maximum=3600.0, field="min_delay_seconds")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise Crawl4AIConfigError("user_agent is required")
        if not isinstance(self.follow_links, bool):
            raise Crawl4AIConfigError("follow_links must be a boolean")
        _integer_range(
            self.max_follow_up_pages,
            minimum=0,
            maximum=MAX_FOLLOW_UP_PAGES,
            field="max_follow_up_pages",
        )
        fields = self.follow_record_fields
        if isinstance(fields, str) or not isinstance(fields, Sequence):
            raise Crawl4AIConfigError("follow_record_fields must be an array")
        normalized_fields = tuple(str(item).strip() for item in fields if str(item).strip())
        if any(item not in FOLLOW_RECORD_FIELDS for item in normalized_fields):
            raise Crawl4AIConfigError(
                "follow_record_fields may only contain: " + ", ".join(sorted(FOLLOW_RECORD_FIELDS))
            )
        if len(set(normalized_fields)) != len(normalized_fields):
            raise Crawl4AIConfigError("follow_record_fields must not contain duplicates")
        object.__setattr__(self, "follow_record_fields", normalized_fields)
        if self.follow_parser is not None:
            if not isinstance(self.follow_parser, str) or self.follow_parser not in PARSER_CONTRACTS:
                raise Crawl4AIConfigError(
                    "follow_parser must be one of: " + ", ".join(sorted(PARSER_CONTRACTS))
                )
        if self.follow_up_min_delay_seconds is not None:
            if isinstance(self.follow_up_min_delay_seconds, bool):
                raise Crawl4AIConfigError("follow_up_min_delay_seconds must be numeric")
            _finite_range(
                self.follow_up_min_delay_seconds,
                minimum=0.0,
                maximum=3600.0,
                field="follow_up_min_delay_seconds",
            )
        if not self.follow_links and self.max_follow_up_pages:
            raise Crawl4AIConfigError(
                "max_follow_up_pages requires follow_links=true; keep link expansion explicitly opt-in"
            )
        if self.follow_links and self.max_follow_up_pages and not normalized_fields:
            raise Crawl4AIConfigError(
                "follow_record_fields is required when follow_links is enabled"
            )


def _central_rights_decision_for_values(
    *,
    source_id: object,
    license_status: object,
    rights_reference: object,
    enabled: object = True,
    test_only: object = False,
) -> source_rights.Decision:
    """Evaluate the authoritative v260 network decision for one page lane.

    The operator's licence fields are deliberately passed as *untrusted*
    context.  ``source_rights`` records them as ignored inputs and never uses
    them to uplift a ``BLOCK`` or ``FUTURE_DISABLED`` policy result.
    """

    try:
        return source_rights.require_source_rights(
            str(source_id).strip() if isinstance(source_id, str) else "",
            source_rights.UseCase.NETWORK_FETCH,
            authorization_reference=rights_reference,
            operator_registry={
                "source_id": source_id,
                "license_status": license_status,
            },
            config={
                "enabled": enabled,
                "test_only": test_only,
                "license_status": license_status,
                "rights_reference": rights_reference,
            },
        )
    except source_rights.SourceRightsBlocked as exc:
        return exc.decision


def _central_rights_decision(config: Crawl4AIConfig) -> source_rights.Decision:
    """Evaluate the authoritative v260 decision for a parsed config."""

    return _central_rights_decision_for_values(
        source_id=config.source_id,
        license_status=config.license_status,
        rights_reference=config.rights_reference,
        enabled=config.enabled,
        test_only=config.test_only,
    )


def _host_belongs_to_other_declared_source(host: str, source_id: str) -> bool:
    """Return whether ``host`` is explicitly owned by another parser lane."""

    normalized = host.lower().rstrip(".")
    for candidate_id, hosts in SOURCE_IDENTITY_HOSTS.items():
        if candidate_id == source_id:
            continue
        if _host_allowed(normalized, tuple(hosts)):
            return True
    return False


def _parser_source_mismatch(
    config: Crawl4AIConfig,
    *,
    parser_contract: str | None = None,
    url: str | None = None,
) -> str | None:
    """Return a stable reason when parser, source, and host do not agree.

    ``parser_contract`` is optional so the same check can be applied first to
    the declared config and then to the parser actually selected by ``auto``
    after a response is returned.  Unknown IDs remain available only for the
    explicit test-only fixture seam; policy-known IDs without a Crawl4AI
    contract (such as OpenFootball) are never browser identities.
    """

    source_id = str(config.source_id or "").strip()
    if not source_id:
        # Anonymous pages are retained as display-only legacy evidence.  The
        # central rights gate still blocks them in production because an empty
        # source ID has no v260 ALLOW decision.
        return None

    contract = config.parser if parser_contract is None else parser_contract
    allowed = SOURCE_PARSER_CONTRACTS.get(source_id)
    if allowed is None:
        if source_id in _KNOWN_POLICY_SOURCE_IDS:
            return f"source_{source_id}_has_no_crawl4ai_parser_contract"
        if not config.test_only:
            return f"source_{source_id}_is_unregistered_for_crawl4ai"
        # Unknown IDs are deliberately retained for deterministic fixture
        # tests, where the central policy is monkeypatched to an explicit
        # ALLOW and no runtime evidence is emitted.
        return None
    if contract not in allowed:
        return f"parser_{contract}_not_allowed_for_{source_id}"

    # ``allowed_hosts`` is operator configuration and may be broadened for a
    # test fixture.  A registered source still cannot be routed through a
    # host explicitly owned by another source.  Unknown fixture hosts remain
    # compatible with the existing test-only seam; production lanes reject
    # any host outside their source identity table.
    parsed_host = (urlparse(url or config.url).hostname or "").lower().rstrip(".")
    expected_hosts = SOURCE_IDENTITY_HOSTS.get(source_id)
    if expected_hosts and not _host_allowed(parsed_host, tuple(expected_hosts)):
        declared_host = (urlparse(config.url).hostname or "").lower().rstrip(".")
        declared_host_owned = _host_allowed(declared_host, tuple(expected_hosts))
        # Test-only fixtures may use a synthetic host, but a fixture that
        # starts on the real provider host must not redirect to an unrelated
        # operator-allowlisted host and retain the provider identity.  This
        # keeps the test seam permissive without making it a relabelling
        # bypass.
        if (
            not config.test_only
            or declared_host_owned
            or _host_belongs_to_other_declared_source(parsed_host, source_id)
        ):
            return f"source_host_{parsed_host or 'missing'}_not_allowed_for_{source_id}"
    return None


def _config_rights_declaration_allowed(config: Crawl4AIConfig) -> bool:
    """Require a bounded operator declaration in addition to central ALLOW."""

    return config.license_status in {"open", "authorized"} and bool(
        config.rights_reference
    )


def _finite_range(value: Any, *, minimum: float, maximum: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise Crawl4AIConfigError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise Crawl4AIConfigError(f"{field} must be between {minimum:g} and {maximum:g}")
    return parsed


def _integer_range(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise Crawl4AIConfigError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    normalized = host.lower().rstrip(".")
    for candidate in allowed_hosts:
        item = str(candidate).lower().rstrip(".")
        if item.startswith("*.") and normalized.endswith(item[1:]):
            return True
        if normalized == item:
            return True
    return False


def _validate_url(url: str, config: Crawl4AIConfig | None = None) -> ParseResult:
    if not isinstance(url, str) or not url.strip():
        raise Crawl4AIConfigError("url is required")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise Crawl4AIConfigError("crawl URL must be an HTTPS URL")
    if parsed.username or parsed.password:
        raise Crawl4AIConfigError("crawl URL must not contain credentials")
    if config is not None:
        if not _host_allowed(parsed.hostname, config.allowed_hosts):
            raise Crawl4AIConfigError(f"URL host is outside the allowlist: {parsed.hostname}")
        path = parsed.path or "/"
        if not any(path.startswith(prefix) for prefix in config.allowed_path_prefixes):
            raise Crawl4AIConfigError(f"URL path is outside the allowlist: {path}")
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # noqa: N802
        self,
        req: Any,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: Any,
    ) -> None:
        raise urllib.error.URLError("redirects are disabled for robots checks")


def _environment_proxy(
    *, supported_schemes: Sequence[str] = ("http", "https", "socks5", "socks5h")
) -> str | None:
    """Return one explicit environment proxy without logging its value.

    Crawl4AI's browser does not automatically inherit the proxy that ordinary
    HTTP clients use on this host.  Forwarding an operator-provided proxy is a
    transport configuration, not an access-control bypass: robots, URL
    allowlists, redirect checks, and strict TLS remain enforced below.  The
    value is intentionally kept out of returned diagnostics because it may
    contain credentials.
    """

    allowed = {str(scheme).lower() for scheme in supported_schemes}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
            scheme = (parsed.scheme or "").lower()
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            continue
        if scheme not in allowed or not hostname:
            continue
        if port is not None and not 1 <= port <= 65535:
            continue
        return value
    return None


def _robots_from_network(config: Crawl4AIConfig) -> dict[str, Any]:
    """Fetch one same-host robots file with a bounded, no-redirect client."""

    parsed = urlparse(config.url)
    robots_url = f"https://{parsed.hostname}/robots.txt"
    request = urllib.request.Request(robots_url, headers={"User-Agent": config.user_agent})
    # urllib has no built-in SOCKS handler.  Use only an explicitly configured
    # HTTP(S) proxy for the robots preflight; the browser may still use a
    # SOCKS5 proxy through Crawl4AI's own BrowserConfig.
    robots_proxy = _environment_proxy(supported_schemes=("http", "https"))
    proxy_handler = urllib.request.ProxyHandler(
        {"http": robots_proxy, "https": robots_proxy} if robots_proxy else {}
    )
    opener = urllib.request.build_opener(_NoRedirect(), proxy_handler)
    try:
        with opener.open(request, timeout=min(config.timeout_seconds, 15.0)) as response:
            status = int(getattr(response, "status", response.getcode()))
            payload = response.read(ROBOTS_MAX_BYTES + 1)
            if len(payload) > ROBOTS_MAX_BYTES:
                return {"allowed": False, "status": "oversized", "url": robots_url}
            if status < 200 or status >= 300:
                return {"allowed": False, "status": f"http_{status}", "url": robots_url}
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(payload.decode("utf-8", errors="replace").splitlines())
            return {
                "allowed": bool(parser.can_fetch(config.user_agent, config.url)),
                "status": "allowed" if parser.can_fetch(config.user_agent, config.url) else "disallowed",
                "url": robots_url,
            }
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            # A missing robots file does not grant permission to ignore other
            # site policy, but it is not a robots denial either.
            return {"allowed": True, "status": "not_found", "url": robots_url}
        return {"allowed": False, "status": f"http_{exc.code}", "url": robots_url}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"allowed": False, "status": "error", "url": robots_url, "error": str(exc)}


def _robots_result(config: Crawl4AIConfig, checker: Callable[..., Any] | None) -> dict[str, Any]:
    if not config.check_robots:
        return {"allowed": False, "status": "not_checked", "reason": "robots_check_required"}
    if checker is None:
        return _robots_from_network(config)
    try:
        value = checker(config.url, config.user_agent)
    except TypeError:
        value = checker(config.url)
    if isinstance(value, Mapping):
        result = dict(value)
        result["allowed"] = bool(result.get("allowed"))
        result.setdefault("status", "allowed" if result["allowed"] else "denied")
        return result
    return {"allowed": bool(value), "status": "allowed" if value else "disallowed"}


def _browser_timezone_for_config(config: Crawl4AIConfig) -> str | None:
    """Return the explicit browser timezone required by a fixture contract.

    WhoScored and the Premier League render human-readable local kickoff
    labels.  Their parser contracts interpret those labels as London time, so
    the browser context must use the same zone.  LaLiga emits ISO-8601
    ``startDate`` values and generic pages carry no time contract; leave those
    contexts unchanged.
    """

    parser = config.parser
    if parser in {
        "whoscored_public_fixtures_v1",
        "premier_league_public_fixtures_v1",
    }:
        return PUBLIC_DISPLAY_TIMEZONE
    if parser != "auto":
        return None
    host = (urlparse(config.url).hostname or "").lower().rstrip(".")
    if host == "whoscored.com" or host.endswith(".whoscored.com"):
        return PUBLIC_DISPLAY_TIMEZONE
    if host == "premierleague.com" or host.endswith(".premierleague.com"):
        return PUBLIC_DISPLAY_TIMEZONE
    return None


async def _default_crawler_runner(config: Crawl4AIConfig) -> Any:
    """Run Crawl4AI with no LLM extraction and a bounded browser config."""

    tls_runtime = strict_tls_runtime_status()
    if tls_runtime["status"] != "supported":
        raise RuntimeError(f"strict TLS runtime unavailable: {tls_runtime.get('reason', tls_runtime['status'])}")

    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser_kwargs: dict[str, Any] = {
        "headless": True,
        "java_script_enabled": True,
        # Use Crawl4AI's normal BrowserManager path.  Its native launch path
        # correctly forwards the configured Chrome channel; the persistent
        # context path in 0.9.x does not, which can select a missing bundled
        # headless shell even when system Chrome is available.
        "use_persistent_context": False,
        # The preflight above proves that this Crawl4AI build does not add
        # certificate-bypass flags after the local filter is applied.
        # Matchline must let invalid certificates fail and classify the
        # source; it never weakens TLS to make a page appear healthy.
        "ignore_https_errors": False,
        "user_agent": config.user_agent,
    }
    # Crawl4AI/Playwright normally expect a downloaded Chromium build.  A
    # machine may already provide a system Chrome, though; using its named
    # Playwright channel avoids a second browser download without weakening
    # any URL, robots, redirect, or source-license boundary.  If no channel
    # is present we leave the default Chromium selection intact so the error
    # remains an explicit browser-readiness failure.
    if any(shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")):
        browser_kwargs["chrome_channel"] = "chrome"
    browser_proxy = _environment_proxy()
    if browser_proxy:
        # BrowserConfig accepts http(s) and socks5 proxies through its current
        # proxy_config API.  The value is not emitted in snapshots or logs
        # because it can contain credentials.
        browser_kwargs["proxy_config"] = {"server": browser_proxy}
    browser_config = BrowserConfig(**browser_kwargs)
    run_kwargs: dict[str, Any] = {
        "check_robots_txt": True,
        "page_timeout": int(config.timeout_seconds * 1000),
        "word_count_threshold": 1,
    }
    display_timezone = _browser_timezone_for_config(config)
    if display_timezone is not None:
        # Both public fixture-card contracts expose a localised clock rather
        # than an embedded offset.  Pin the browser context so a workstation
        # timezone cannot silently turn London kickoffs into Asia/Shanghai
        # labels before the exact canonical-fixture join.
        run_kwargs["timezone_id"] = display_timezone
    if config.parser == "premier_league_public_fixtures_v1":
        # The official page fills match cards asynchronously after the 302
        # landing route.  Wait for the source-owned card marker, but keep the
        # wait bounded so an unavailable page is reported as a source error.
        run_kwargs["wait_for"] = PREMIER_LEAGUE_MATCH_CARD_SELECTOR
        run_kwargs["wait_for_timeout"] = min(
            int(config.timeout_seconds * 1000), 10_000
        )
    cache_mode = getattr(CacheMode, "BYPASS", None)
    if cache_mode is not None:
        run_kwargs["cache_mode"] = cache_mode
    run_config = CrawlerRunConfig(**run_kwargs)
    # Crawl4AI 0.9.x adds the two unsafe flags in both browser paths.  Keep
    # the filter active through __aenter__ and __aexit__ so no browser process
    # can be created from the unfiltered third-party methods.
    with _strict_tls_compat_patch():
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await crawler.arun(config.url, config=run_config)


async def _invoke_runner(runner: Callable[[Crawl4AIConfig], Any], config: Crawl4AIConfig) -> Any:
    value = runner(config)
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke_runner_with_deadline(
    runner: Callable[[Crawl4AIConfig], Any],
    config: Crawl4AIConfig,
    *,
    timeout: float,
) -> Any:
    """Run one vendor call without confusing its TimeoutError with ours."""

    task = asyncio.create_task(_invoke_runner(runner, config))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _RunnerDeadlineExceeded
    return task.result()


def _run_async(awaitable: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("fetch_crawl4ai must be called outside a running event loop")


def _runner_timeout_seconds(config: Crawl4AIConfig) -> float:
    """Bound the whole Crawl4AI call, including vendor-side DOM parsing.

    ``page_timeout`` covers browser navigation, but Crawl4AI can still spend
    unbounded time materialising a very large result after navigation returns.
    Keep a small, proportional cancellation grace so one changed public page
    cannot hold the serial source worker until systemd kills the whole cycle.
    """

    grace = min(RUNNER_TIMEOUT_MAX_GRACE_SECONDS, max(1.0, float(config.timeout_seconds) * 0.25))
    return float(config.timeout_seconds) + grace


def _header_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).lower(): str(item)
        for key, item in value.items()
        if isinstance(key, str) and item is not None
    }


def _markdown_value(result: Any) -> str:
    value = getattr(result, "markdown", "")
    if isinstance(value, str):
        # Crawl4AI 0.9.x exposes ``StringCompatibleMarkdown`` here.  It is a
        # ``str`` subclass for rendering, but its deepcopy can become a
        # ``MarkdownGenerationResult`` object.  Convert it to a plain string
        # before the page reaches the immutable ledger or JSON encoder.
        return str(value)
    for key in ("raw_markdown", "fit_markdown"):
        candidate = getattr(value, key, None)
        if isinstance(candidate, str):
            return str(candidate)
    if isinstance(value, Mapping):
        for key in ("raw_markdown", "fit_markdown"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return str(candidate)
    return ""


def _metadata_date(result: Any, observed_at: datetime) -> tuple[str | None, str]:
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None, "observed_at_fallback"
    for key in ("datePublished", "date_published", "published_at", "published", "article:published_time"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= observed_at:
            return parsed.isoformat(), f"metadata:{key}"
    return None, "observed_at_fallback"


def _safe_excerpt(value: str, limit: int = DEFAULT_MAX_MARKDOWN_BYTES) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


class _JsonLdScriptParser(HTMLParser):
    """Collect bounded JSON-LD script blocks without executing page code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buffer: list[str] | None = None
        self._bytes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.blocks) >= MAX_JSON_LD_SCRIPTS or self._buffer is not None or tag.lower() != "script":
            return
        attributes = {str(key).lower(): str(value or "").lower() for key, value in attrs}
        script_type = attributes.get("type", "").split(";", 1)[0].strip()
        if script_type == "application/ld+json":
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is None:
            return
        encoded = data.encode("utf-8")
        remaining = MAX_JSON_LD_BYTES - self._bytes
        if remaining <= 0:
            return
        chunk = encoded[:remaining].decode("utf-8", errors="ignore")
        self._buffer.append(chunk)
        self._bytes += len(chunk.encode("utf-8"))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._buffer is None:
            return
        self.blocks.append("".join(self._buffer))
        self._buffer = None


def _structured_facts(raw_html: str) -> list[dict[str, Any]]:
    """Extract bounded JSON-LD facts for display/audit, never model input.

    JSON-LD is treated as a claim made by the page, not as an authoritative
    match fact.  The caller must still perform source-specific parsing,
    entity matching, and freeze-time checks before any model admission.
    """

    parser = _JsonLdScriptParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return []
    facts: list[dict[str, Any]] = []
    for block in parser.blocks:
        try:
            value = json.loads(block)
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        if isinstance(value, Mapping) and isinstance(value.get("@graph"), list):
            values = value["@graph"]
        for item in values[:MAX_JSON_LD_SCRIPTS]:
            if not isinstance(item, Mapping):
                continue
            # Keep only low-risk, source-declared metadata.  Do not copy the
            # full page graph or any script payload into the ledger.
            selected = {
                key: _json_safe(item.get(key))
                for key in (
                    "@type",
                    "@id",
                    "headline",
                    "name",
                    "description",
                    "datePublished",
                    "dateModified",
                    "startDate",
                    "endDate",
                    "eventStatus",
                    "url",
                    "author",
                    "publisher",
                    "homeTeam",
                    "awayTeam",
                    "location",
                )
                if key in item
            }
            if selected:
                facts.append(selected)
            if len(facts) >= MAX_JSON_LD_SCRIPTS:
                return facts
    return facts


def _structured_sports_event_records(raw_html: str, *, parser_name: str) -> list[dict[str, Any]]:
    """Project source-declared ``SportsEvent`` JSON-LD into bounded evidence.

    JSON-LD remains a page claim: the returned records are never canonical
    fixtures and never model features.  The projection is intentionally
    narrow so a source-specific join can be added later without forwarding
    the page's full structured graph into the ledger.
    """

    def team_name(value: Any) -> str | None:
        if isinstance(value, Mapping):
            candidate = value.get("name")
        else:
            candidate = value
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        return " ".join(candidate.split())

    def location_name(value: Any) -> str | None:
        if not isinstance(value, Mapping):
            return None
        candidate = value.get("name")
        return " ".join(candidate.split()) if isinstance(candidate, str) and candidate.strip() else None

    records: list[dict[str, Any]] = []
    for fact in _structured_facts(raw_html):
        kind = fact.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "SportsEvent" not in kinds:
            continue
        home = team_name(fact.get("homeTeam"))
        away = team_name(fact.get("awayTeam"))
        kickoff = fact.get("startDate")
        if not home or not away or not isinstance(kickoff, str) or not kickoff.strip():
            continue
        event_url = fact.get("url")
        event_id = fact.get("@id") or event_url
        source_event_id = (
            str(event_id).strip()
            if isinstance(event_id, str) and event_id.strip()
            else None
        )
        record = {
            "source_event_id": source_event_id,
            "match_url": event_url if isinstance(event_url, str) and event_url.strip() else None,
            "kickoff_at": kickoff.strip(),
            "home_team": home,
            "away_team": away,
            "venue": location_name(fact.get("location")),
            "status": fact.get("eventStatus") if isinstance(fact.get("eventStatus"), str) else "scheduled",
            "source_format": "schema.org SportsEvent JSON-LD",
            "enters_model": False,
            "model_exclusion_reason": "source_parser_display_only_requires_fixture_join_and_license_review",
        }
        dedupe_key = (source_event_id or "") + "|" + home + "|" + away + "|" + kickoff
        if any(str(item.get("_dedupe_key")) == dedupe_key for item in records):
            continue
        record["_dedupe_key"] = dedupe_key
        records.append(record)
        if len(records) >= MAX_EXTRACTED_RECORDS:
            break
    for record in records:
        record.pop("_dedupe_key", None)
    return records


class _FootballDataArchiveParser(HTMLParser):
    """Index public Football-Data CSV links without downloading the archive.

    Football-Data is a historical archive.  The page itself is useful for
    provenance and operator visibility, but the CSV links are not a future
    observation and must never be treated as a live pre-match feature.  Keep
    this parser deliberately narrow: only same-host HTTPS links ending in
    ``.csv`` under the archive's ``/mmz.../`` path are projected.
    """

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._capture: dict[str, Any] | None = None
        self.records: list[dict[str, Any]] = []
        parsed = urlparse(base_url)
        self._base_host = (parsed.hostname or "").lower().rstrip(".")

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())[:160]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._capture is not None:
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        href = values.get("href", "").strip()
        if not href:
            return
        parsed = urlparse(urljoin(self.base_url, href))
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or ""
        if (
            parsed.scheme != "https"
            or host != self._base_host
            or not path.lower().endswith(".csv")
            or not re.fullmatch(r"/mmz[^/]+/[^/]+/[^/]+\.csv", path, flags=re.IGNORECASE)
        ):
            return
        self._capture = {"url": parsed._replace(fragment="").geturl(), "buffer": []}

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["buffer"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._capture is None:
            return
        url = str(self._capture.get("url") or "")
        label = self._clean("".join(self._capture.get("buffer") or []))
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split("/") if part]
        season_code = path_parts[-2] if len(path_parts) >= 2 else None
        filename = path_parts[-1] if path_parts else ""
        competition_code = filename.rsplit(".", 1)[0] if "." in filename else None
        dedupe_key = url.lower()
        if not any(str(item.get("data_url", "")).lower() == dedupe_key for item in self.records):
            self.records.append(
                {
                    "data_url": url,
                    "label": label or None,
                    "season_code": season_code,
                    "competition_code": competition_code,
                    "dataset_kind": "csv",
                    "source_format": "same-host HTML archive link",
                    "historical_only": True,
                    "enters_model": False,
                    "model_exclusion_reason": "historical_archive_training_and_backtest_only_not_future_observation",
                }
            )
        self._capture = None

    def output(self) -> list[dict[str, Any]]:
        return self.records[:MAX_EXTRACTED_RECORDS]


def _football_data_archive_records(raw_html: str, *, base_url: str) -> list[dict[str, Any]]:
    parser = _FootballDataArchiveParser(base_url=base_url)
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return []
    return parser.output()


def _iter_json_ld_nodes(
    value: Any, *, max_nodes: int = 500
) -> Iterator[Mapping[str, Any]]:
    """Yield bounded nested JSON-LD objects without traversing page payloads.

    News pages commonly wrap NewsArticle objects inside an ItemList and then
    a ListItem.item object. The generic metadata projection is intentionally
    shallow, so this helper follows only the small set of schema.org
    container keys needed for a declared news parser. It never walks
    arbitrary dictionary values or executes page code.
    """

    seen: set[int] = set()
    yielded = 0

    def walk(item: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
        nonlocal yielded
        if depth > 8 or yielded >= max_nodes:
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            yielded += 1
            yield item
            for key in ("@graph", "itemListElement", "item", "mainEntity"):
                child = item.get(key)
                if child is None:
                    continue
                yield from walk(child, depth + 1)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                yield from walk(child, depth + 1)

    yield from walk(value)


def _structured_news_article_records(raw_html: str, *, base_url: str) -> list[dict[str, Any]]:
    """Extract source-declared NewsArticle JSON-LD as display evidence.

    Some official league pages publish a real article feed in JSON-LD but do
    not expose article URLs in the structured object. We retain the exact
    headline, source-declared publication time, and a stable evidence ID in
    that case; a missing URL is kept as None rather than guessed from a page
    route. The records are never fixture/team facts or model features.
    """

    parser = _JsonLdScriptParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return []

    base = urlparse(base_url)
    base_host = (base.hostname or "").lower().rstrip(".")

    def clean(value: Any, limit: int) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return " ".join(value.split())[:limit]

    def same_host_url(value: Any) -> str | None:
        candidate = value.get("@id") if isinstance(value, Mapping) else value
        if isinstance(value, Mapping) and not candidate:
            candidate = value.get("url")
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        parsed = urlparse(urljoin(base_url, candidate.strip()))
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host != base_host:
            return None
        normalized = parsed._replace(fragment="").geturl()
        # A NewsArticle's generic mainEntityOfPage often points back to the
        # source homepage. It is not an article URL and must not be presented
        # as one.
        if normalized.rstrip("/") == base_url.rstrip("/") or parsed.path in {"", "/"}:
            return None
        return normalized

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for block in parser.blocks:
        try:
            value = json.loads(block)
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _iter_json_ld_nodes(value):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "NewsArticle" not in kinds:
                continue
            headline = clean(item.get("headline"), 300)
            if not headline:
                continue
            published_at = clean(item.get("datePublished") or item.get("published_at"), 80)
            modified_at = clean(item.get("dateModified") or item.get("modified_at"), 80)
            article_url = same_host_url(item.get("url")) or same_host_url(item.get("@id"))
            if not article_url:
                article_url = same_host_url(item.get("mainEntityOfPage"))
            source_article_id = article_url or (
                "jsonld:"
                + hashlib.sha256(
                    f"{base_url}|{headline}|{published_at or ''}".encode("utf-8")
                ).hexdigest()
            )
            if source_article_id in seen_ids:
                continue
            seen_ids.add(source_article_id)
            publisher = item.get("publisher")
            publisher_name = clean(
                publisher.get("name") if isinstance(publisher, Mapping) else publisher,
                120,
            )
            records.append(
                {
                    "source_article_id": source_article_id,
                    "article_url": article_url,
                    "source_page_url": base_url,
                    "headline": headline,
                    "summary": clean(item.get("description"), 500),
                    "published_at": published_at,
                    "modified_at": modified_at,
                    "publisher": publisher_name,
                    "source_format": "schema.org NewsArticle JSON-LD",
                    "time_semantics": "source_declared_publication_time",
                    "enters_model": False,
                    "model_exclusion_reason": "news_page_display_only_requires_fixture_or_team_join_and_time_cutoff",
                }
            )
            if len(records) >= MAX_EXTRACTED_RECORDS:
                return records
    return records


class _WhoScoredFixtureParser(HTMLParser):
    """Extract bounded fixture cards from a public WhoScored league page.

    This is deliberately a display/audit parser, not a feature adapter.  The
    site renders the fixture list as nested ``div`` cards rather than a
    stable JSON endpoint.  We retain the source's date/time labels and odds
    exactly as displayed, and never infer a timezone or turn them into model
    fields here.  A future model adapter still needs an exact fixture join,
    publication-time proof, licensing review, and freeze-stage admission.
    """

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._stack: list[str] = []
        self._date_depth: int | None = None
        self._date_buffer: list[str] = []
        self.current_date_label: str | None = None
        self._match_depth: int | None = None
        self._match: dict[str, Any] | None = None
        self._capture: dict[str, Any] | None = None
        self.records: list[dict[str, Any]] = []

    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    @staticmethod
    def _attrs_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    @staticmethod
    def _has_class(classes: str, prefix: str) -> bool:
        return any(item.startswith(prefix) for item in classes.split())

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    def _number(value: str) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _odds(value: str) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 1.0 else None

    def _start_capture(self, kind: str, depth: int, **extra: Any) -> None:
        # The card markup does not nest the relevant fields.  If a malformed
        # page does, keep the first capture and discard the nested claim
        # rather than accidentally joining text from two fields.
        if self._capture is None:
            self._capture = {"kind": kind, "depth": depth, "buffer": [], **extra}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self._stack)
        values = self._attrs_map(attrs)
        classes = values.get("class", "")
        if self._date_depth is None and self._has_class(classes, "Accordion-module_header__"):
            self._date_depth = depth
            self._date_buffer = []

        if self._match_depth is None and self._has_class(classes, "Match-module_match__"):
            self._match_depth = depth
            self._match = {
                "teams": [],
                "team_links": [],
                "odds": [],
                "score_text": "",
                "kickoff_time": None,
                "match_url": None,
                "source_match_id": None,
            }

        if self._match_depth is not None and self._match is not None:
            element_id = values.get("id", "")
            if tag.lower() == "a" and element_id.startswith("scoresBtn-"):
                self._match["source_match_id"] = element_id.removeprefix("scoresBtn-")
                href = values.get("href", "")
                if href:
                    self._match["match_url"] = urljoin(self.base_url, href)
                self._start_capture("score", depth)
            elif tag.lower() == "a" and self._has_class(classes, "Match-module_teamNameText__"):
                self._start_capture("team", depth, href=values.get("href", ""))
            elif tag.lower() == "span" and self._has_class(classes, "Match-module_startTime__"):
                self._start_capture("kickoff_time", depth)
            elif tag.lower() == "span" and self._has_class(classes, "OddsButton-module_oddsText__"):
                self._start_capture("odds", depth)
        if tag.lower() not in self._VOID_TAGS:
            self._stack.append(tag.lower())

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["buffer"].append(data)
        elif self._date_depth is not None:
            self._date_buffer.append(data)

    def _finish_capture(self) -> None:
        if self._capture is None or self._match is None:
            return
        capture = self._capture
        value = self._clean("".join(capture["buffer"]))
        kind = capture["kind"]
        if kind == "team" and value:
            self._match["teams"].append(value)
            self._match["team_links"].append(urljoin(self.base_url, str(capture.get("href") or "")))
        elif kind == "score":
            self._match["score_text"] = value
        elif kind == "kickoff_time":
            self._match["kickoff_time"] = value or None
        elif kind == "odds":
            parsed = self._odds(value)
            if parsed is not None:
                self._match["odds"].append(parsed)
        self._capture = None

    def _finish_match(self) -> None:
        match = self._match
        if not isinstance(match, dict):
            return
        teams = [str(item) for item in match.get("teams", []) if str(item).strip()]
        if len(teams) < 2:
            return
        score_tokens = [item for item in str(match.get("score_text") or "").replace("–", "-").split() if item]
        score_values = [self._number(item) for item in score_tokens[:2]]
        while len(score_values) < 2:
            score_values.append(None)
        odds = list(match.get("odds") or [])[:3]
        while len(odds) < 3:
            odds.append(None)
        source_match_id = str(match.get("source_match_id") or "").strip() or None
        record = {
            "source_match_id": source_match_id,
            "match_url": match.get("match_url"),
            "date_label": self.current_date_label,
            "kickoff_time_label": match.get("kickoff_time"),
            "time_semantics": "source_display_label_in_browser_context_timezone",
            "display_timezone": PUBLIC_DISPLAY_TIMEZONE,
            "home_team": teams[0],
            "away_team": teams[1],
            "home_score": score_values[0],
            "away_score": score_values[1],
            "status": "scheduled" if score_values == [None, None] else "score_displayed",
            "one_x_two_odds": {
                "home": odds[0],
                "draw": odds[1],
                "away": odds[2],
            },
            "enters_model": False,
            "model_exclusion_reason": "source_parser_display_only_requires_fixture_join_and_license_review",
        }
        dedupe_key = source_match_id or f"{record['date_label']}|{record['home_team']}|{record['away_team']}|{record['kickoff_time_label']}"
        if not any(item.get("_dedupe_key") == dedupe_key for item in self.records):
            record["_dedupe_key"] = dedupe_key
            self.records.append(record)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._VOID_TAGS or not self._stack:
            return
        depth = len(self._stack) - 1
        if self._capture is not None and self._capture.get("depth") == depth:
            self._finish_capture()
        if self._date_depth == depth:
            label = self._clean("".join(self._date_buffer))
            if label:
                self.current_date_label = label
            self._date_depth = None
            self._date_buffer = []
        if self._match_depth == depth:
            self._finish_match()
            self._match_depth = None
            self._match = None
        self._stack.pop()

    def output(self) -> list[dict[str, Any]]:
        for record in self.records[:MAX_EXTRACTED_RECORDS]:
            record.pop("_dedupe_key", None)
        return self.records[:MAX_EXTRACTED_RECORDS]


class _PremierLeagueMatchParser(HTMLParser):
    """Extract the official page's bounded match-card display contract."""

    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._stack: list[str] = []
        self._day_depth: int | None = None
        self._day_buffer: list[str] = []
        self.current_day_label: str | None = None
        self._card_depth: int | None = None
        self._card: dict[str, Any] | None = None
        self._capture: dict[str, Any] | None = None
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def _attrs_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())

    def _start_capture(self, kind: str, depth: int) -> None:
        if self._capture is None:
            self._capture = {"kind": kind, "depth": depth, "buffer": []}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self._stack)
        values = self._attrs_map(attrs)
        test_id = values.get("data-testid", "")
        if self._card_depth is None and test_id == "dayDate":
            self._day_depth = depth
            self._day_buffer = []
        if self._card_depth is None and tag.lower() == "a" and test_id == "matchCard":
            href = values.get("href", "")
            match_id = None
            match = re.search(r"/match/(\d+)(?:/|$)", href)
            if match:
                match_id = match.group(1)
            self._card_depth = depth
            self._card = {
                "source_match_id": match_id,
                "match_url": urljoin(self.base_url, href) if href else None,
                "teams": [],
                "kickoff_time": None,
            }
        if self._card_depth is not None and self._card is not None and test_id in {
            "matchCardTeamFullName",
            "matchCardKickoffTime",
        }:
            self._start_capture("team" if test_id == "matchCardTeamFullName" else "kickoff", depth)
        if tag.lower() not in self._VOID_TAGS:
            self._stack.append(tag.lower())

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["buffer"].append(data)
        elif self._day_depth is not None:
            self._day_buffer.append(data)

    def _finish_capture(self) -> None:
        if self._capture is None or self._card is None:
            return
        value = self._clean("".join(self._capture["buffer"]))
        if self._capture["kind"] == "team" and value:
            self._card["teams"].append(value)
        elif self._capture["kind"] == "kickoff" and value:
            self._card["kickoff_time"] = value
        self._capture = None

    def _finish_card(self) -> None:
        card = self._card
        if not isinstance(card, dict):
            return
        teams = [str(item).strip() for item in card.get("teams", []) if str(item).strip()]
        if len(teams) < 2:
            return
        record = {
            "source_match_id": card.get("source_match_id"),
            "match_url": card.get("match_url"),
            "date_label": self.current_day_label,
            "kickoff_time_label": card.get("kickoff_time"),
            "time_semantics": "source_display_label_in_browser_context_timezone",
            "display_timezone": PUBLIC_DISPLAY_TIMEZONE,
            "home_team": teams[0],
            "away_team": teams[1],
            "status": "scheduled",
            "enters_model": False,
            "model_exclusion_reason": "source_parser_display_only_requires_fixture_join_and_license_review",
        }
        dedupe_key = record["source_match_id"] or f"{record['date_label']}|{teams[0]}|{teams[1]}|{record['kickoff_time_label']}"
        if not any(item.get("_dedupe_key") == dedupe_key for item in self.records):
            record["_dedupe_key"] = dedupe_key
            self.records.append(record)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._VOID_TAGS or not self._stack:
            return
        depth = len(self._stack) - 1
        if self._capture is not None and self._capture.get("depth") == depth:
            self._finish_capture()
        if self._day_depth == depth:
            label = self._clean("".join(self._day_buffer))
            if label:
                self.current_day_label = label
            self._day_depth = None
            self._day_buffer = []
        if self._card_depth == depth:
            self._finish_card()
            self._card_depth = None
            self._card = None
        self._stack.pop()

    def output(self) -> list[dict[str, Any]]:
        for record in self.records[:MAX_EXTRACTED_RECORDS]:
            record.pop("_dedupe_key", None)
        return self.records[:MAX_EXTRACTED_RECORDS]


class _OfficialNewsCardParser(HTMLParser):
    """Extract bounded headline cards from an explicitly declared news page.

    This parser intentionally does not infer a team, fixture, injury, or
    sentiment label.  It keeps only source-owned headline/link/time evidence
    so researchers can inspect a first-party news surface.  A later adapter
    must perform a fixture or team join and a causal time check before the
    evidence could become a model feature.
    """

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.base_host = (urlparse(base_url).hostname or "").lower().rstrip(".")
        self._stack: list[str] = []
        self._article_depth: int | None = None
        self._article: dict[str, Any] | None = None
        self._capture: dict[str, Any] | None = None
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())

    def _start_capture(self, kind: str, depth: int) -> None:
        if self._capture is None:
            self._capture = {"kind": kind, "depth": depth, "buffer": []}

    def _finish_capture(self) -> None:
        if self._capture is None or self._article is None:
            return
        value = self._clean("".join(self._capture["buffer"]))
        kind = self._capture["kind"]
        if value:
            if kind == "headline" and not self._article.get("headline"):
                self._article["headline"] = value
            elif kind == "anchor" and not self._article.get("anchor_text"):
                self._article["anchor_text"] = value
            elif kind == "summary" and not self._article.get("summary"):
                self._article["summary"] = value[:500]
            elif kind == "published_label" and not self._article.get("published_label"):
                self._article["published_label"] = value[:120]
        self._capture = None

    def _finish_article(self) -> None:
        article = self._article
        if not isinstance(article, dict):
            return
        headline = self._clean(str(article.get("headline") or article.get("anchor_text") or ""))
        href = article.get("href")
        if not headline or not isinstance(href, str) or not href.strip():
            return
        article_url = urljoin(self.base_url, href.strip())
        parsed = urlparse(article_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host != self.base_host:
            return
        record = {
            "source_article_id": article_url,
            "article_url": article_url,
            "headline": headline[:300],
            "summary": str(article.get("summary") or "")[:500] or None,
            "published_at": article.get("published_at"),
            "published_label": str(article.get("published_label") or "")[:120] or None,
            "source_format": "HTML article card",
            "enters_model": False,
            "model_exclusion_reason": "news_page_display_only_requires_fixture_or_team_join_and_time_cutoff",
        }
        dedupe_key = record["source_article_id"]
        if not any(item.get("_dedupe_key") == dedupe_key for item in self.records):
            record["_dedupe_key"] = dedupe_key
            self.records.append(record)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self._stack)
        attrs_map = {str(key).lower(): str(value or "") for key, value in attrs}
        lower = tag.lower()
        if self._article_depth is None and lower == "article":
            self._article_depth = depth
            self._article = {"href": None, "headline": None, "anchor_text": None, "summary": None, "published_at": None, "published_label": None}
        if self._article is not None:
            if lower == "a" and not self._article.get("href"):
                self._article["href"] = attrs_map.get("href") or None
                self._start_capture("anchor", depth)
            elif lower in {"h1", "h2", "h3", "h4"}:
                if self._capture is not None and self._capture.get("kind") == "anchor":
                    self._finish_capture()
                self._start_capture("headline", depth)
            elif lower == "p":
                if self._capture is not None and self._capture.get("kind") == "anchor":
                    self._finish_capture()
                self._start_capture("summary", depth)
            elif lower == "time":
                if self._capture is not None and self._capture.get("kind") == "anchor":
                    self._finish_capture()
                datetime_value = attrs_map.get("datetime")
                if datetime_value:
                    self._article["published_at"] = datetime_value.strip()
                self._start_capture("published_label", depth)
        if lower not in self._VOID_TAGS:
            self._stack.append(lower)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["buffer"].append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._VOID_TAGS or not self._stack:
            return
        depth = len(self._stack) - 1
        if self._capture is not None and self._capture.get("depth") == depth:
            self._finish_capture()
        if self._article_depth == depth:
            self._finish_article()
            self._article_depth = None
            self._article = None
        self._stack.pop()

    def output(self) -> list[dict[str, Any]]:
        if self._capture is not None:
            self._finish_capture()
        if self._article is not None:
            self._finish_article()
        for record in self.records[:MAX_EXTRACTED_RECORDS]:
            record.pop("_dedupe_key", None)
        return self.records[:MAX_EXTRACTED_RECORDS]


class _SerieAFixtureLinkParser(HTMLParser):
    """Extract the official Serie A match-card links as display evidence."""

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.base_host = (urlparse(base_url).hostname or "").lower().rstrip(".")
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        href = values.get("href", "")
        label = self._clean(values.get("aria-label", ""))
        if not href or not label or " - " not in label:
            return
        article_url = urljoin(self.base_url, href)
        parsed = urlparse(article_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host != self.base_host:
            return
        match = re.search(r"/serie-a/match/([0-9a-f]{32})/", parsed.path, flags=re.IGNORECASE)
        if not match:
            return
        home, away = (part.strip() for part in label.split(" - ", 1))
        if not home or not away:
            return
        record = {
            "source_match_id": match.group(1).lower(),
            "match_url": article_url,
            "home_team": home,
            "away_team": away,
            "status": "scheduled",
            "source_format": "Lega Serie A match-card aria-label",
            "enters_model": False,
            "model_exclusion_reason": "source_parser_display_only_requires_fixture_join_and_time_cutoff",
        }
        if not any(item.get("source_match_id") == record["source_match_id"] for item in self.records):
            self.records.append(record)

    def output(self) -> list[dict[str, Any]]:
        return self.records[:MAX_EXTRACTED_RECORDS]


_SERIEA_MATCH_TIME_RE = re.compile(
    r'\\"matchId\\":\\"serie-a::Football_Match::(?P<match_id>[0-9a-f]{32})\\"'
    r'(?P<body>.{0,4096}?)'
    r'\\"matchDateUtc\\":\\"(?P<kickoff>[^"\\]+)\\"'
    r'(?P<tail>.{0,256}?)'
    r'\\"isUnknownKickOffTime\\":(?P<unknown>true|false)',
    flags=re.IGNORECASE | re.DOTALL,
)


def _seriea_match_kickoffs(raw_html: str) -> dict[str, str]:
    """Extract only source-declared UTC kickoffs from the bounded page state.

    The Lega Serie A page renders match cards without a time in the anchor
    contract, but its escaped application state carries ``matchDateUtc`` and
    an explicit ``isUnknownKickOffTime`` flag.  We use a bounded, source-
    specific pattern rather than attempting to decode the whole page state.
    Missing, unknown, or malformed timestamps are deliberately omitted so a
    team-pair card can never be promoted by a guessed local time.
    """

    kickoffs: dict[str, str] = {}
    if not isinstance(raw_html, str) or not raw_html:
        return kickoffs
    for match in _SERIEA_MATCH_TIME_RE.finditer(raw_html):
        if match.group("unknown").lower() != "false":
            continue
        match_id = match.group("match_id").lower()
        value = match.group("kickoff").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        normalized = parsed.astimezone(timezone.utc).isoformat()
        previous = kickoffs.get(match_id)
        # A duplicate source state is acceptable only when it repeats the
        # same timestamp.  Conflicting declarations stay unresolved.
        if previous is not None and previous != normalized:
            kickoffs.pop(match_id, None)
            continue
        if match_id not in kickoffs:
            kickoffs[match_id] = normalized
    return kickoffs


def _source_specific_records(
    *, source_name: str, final_url: str, raw_html: str, parser_contract: str = "auto"
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return bounded, display-only records for an explicit parser contract.

    ``Crawl4AI`` is deliberately not a source selector.  The caller supplies
    the declared source's parser contract, and ``auto`` is retained only for
    backwards-compatible configurations that predate explicit contracts.
    An unsupported/generic contract records the page without projecting
    facts, so a new page can never become a model feature merely because a
    browser returned HTML.
    """

    if parser_contract not in PARSER_CONTRACTS:
        raise Crawl4AIConfigError(f"unsupported parser contract: {parser_contract}")
    if parser_contract == "football_data_archive_v1":
        return parser_contract, _football_data_archive_records(
            raw_html, base_url=final_url
        )
    if parser_contract == "generic_public_page_v1":
        return "generic_public_page_v1", []
    if parser_contract == "official_news_cards_v1":
        news_parser = _OfficialNewsCardParser(base_url=final_url)
        try:
            news_parser.feed(raw_html)
            news_parser.close()
        except Exception:
            return parser_contract, []
        return parser_contract, news_parser.output()
    if parser_contract == "official_news_jsonld_v1":
        return parser_contract, _structured_news_article_records(raw_html, base_url=final_url)
    if parser_contract == "seriea_public_fixtures_v1":
        seriea_parser = _SerieAFixtureLinkParser(base_url=final_url)
        try:
            seriea_parser.feed(raw_html)
            seriea_parser.close()
        except Exception:
            return parser_contract, []
        records = seriea_parser.output()
        kickoffs = _seriea_match_kickoffs(raw_html)
        for record in records:
            match_id = str(record.get("source_match_id") or "").lower()
            kickoff = kickoffs.get(match_id)
            if kickoff is not None:
                record.update(
                    {
                        "kickoff_at": kickoff,
                        "time_semantics": "source_declared_matchDateUtc",
                        "source_time_field": "matchDateUtc",
                        "source_timezone": "UTC",
                    }
                )
            else:
                record["time_semantics"] = "source_match_card_kickoff_unavailable"
        return parser_contract, records
    if parser_contract == "sports_event_jsonld_v1":
        return "sports_event_jsonld_v1", _structured_sports_event_records(
            raw_html, parser_name="sports_event_jsonld_v1"
        )
    if parser_contract == "whoscored_public_fixtures_v1":
        whoscored_parser = _WhoScoredFixtureParser(base_url=final_url)
        try:
            whoscored_parser.feed(raw_html)
            whoscored_parser.close()
        except Exception:
            return parser_contract, []
        return parser_contract, whoscored_parser.output()
    if parser_contract == "premier_league_public_fixtures_v1":
        premier_league_parser = _PremierLeagueMatchParser(base_url=final_url)
        try:
            premier_league_parser.feed(raw_html)
            premier_league_parser.close()
        except Exception:
            return parser_contract, []
        return parser_contract, premier_league_parser.output()
    if parser_contract == "laliga_public_sports_events_v1":
        return parser_contract, _structured_sports_event_records(
            raw_html, parser_name=parser_contract
        )

    host = (urlparse(final_url).hostname or "").lower().rstrip(".")
    if not (host == "whoscored.com" or host.endswith(".whoscored.com")):
        if host == "premierleague.com" or host.endswith(".premierleague.com"):
            premier_league_parser = _PremierLeagueMatchParser(base_url=final_url)
            parser_name = "premier_league_public_fixtures_v1"
            try:
                premier_league_parser.feed(raw_html)
                premier_league_parser.close()
            except Exception:
                return parser_name, []
            return parser_name, premier_league_parser.output()
        if host == "laliga.com" or host.endswith(".laliga.com"):
            parser_name = "laliga_public_sports_events_v1"
            return parser_name, _structured_sports_event_records(raw_html, parser_name=parser_name)
        return None, []
    whoscored_parser = _WhoScoredFixtureParser(base_url=final_url)
    try:
        whoscored_parser.feed(raw_html)
        whoscored_parser.close()
    except Exception:
        return "whoscored_public_fixtures_v1", []
    return "whoscored_public_fixtures_v1", whoscored_parser.output()


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Keep browser metadata serializable and bounded before it reaches JSON."""

    if depth > 4:
        return str(value)
    if value is None:
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:100]]
    return str(value)


def _failure_code(stage: str, error: str, *, status_code: Any = None, robots: Any = None) -> str:
    """Map an isolated source failure to a stable operator-facing code.

    The code is diagnostic only.  It never upgrades a failed page to a
    successful source and deliberately avoids retry/bypass advice.
    """

    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if status_code in {401, 403}:
            return f"http_{status_code}"
        if status_code == 404:
            return "http_404"
        if status_code == 408:
            return "http_408_timeout"
        if status_code == 429:
            return "http_429_rate_limited"
        if 500 <= status_code <= 599:
            return f"http_{status_code}_server_error"
        if status_code >= 400:
            return f"http_{status_code}"
    if stage == "robots":
        robots_status = str(robots.get("status", "")) if isinstance(robots, Mapping) else ""
        return "robots_denied" if robots_status in {"denied", "disallowed"} else "robots_unverified"
    if stage == "allowlist":
        return "allowlist_rejected"
    if stage == "redirect":
        return "redirect_outside_allowlist"
    if stage == "size":
        return "response_too_large"
    if stage == "content_type":
        return "content_type_mismatch"
    if stage == "parse":
        return "response_empty_or_unparseable"
    message = str(error).lower()
    if any(token in message for token in ("tls", "ssl", "certificate", "handshake")):
        return "tls_error"
    if any(token in message for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in message for token in ("dns", "connection", "network", "socket", "name or service")):
        return "network_error"
    if any(token in message for token in ("crawl4ai", "chromium", "chrome", "browser", "playwright")):
        return "browser_runtime_error"
    return "crawl_error"


def _page_error(config: Crawl4AIConfig, *, observed_at: datetime, stage: str, error: str, **extra: Any) -> dict[str, Any]:
    page_context = extra.pop("page_context", None)
    status_code = extra.get("status_code")
    robots = extra.get("robots")
    error_code = extra.pop("error_code", None) or _failure_code(
        stage,
        error,
        status_code=status_code,
        robots=robots,
    )
    return {
        "source": config.name,
        "source_id": config.source_id,
        "fact_source_id": config.source_id,
        "capture_engine": "Crawl4AI",
        "capture_role": "fetch_runtime",
        "provider_role": "execution_layer",
        "url": config.url,
        "observed_at": observed_at.isoformat(),
        "stage": stage,
        "error_code": error_code,
        "error": str(error),
        "enters_model": False,
        "network_opened": False,
        "model_eligible": False,
        "runtime_evidence": False,
        "test_only": bool(config.test_only),
        "model_exclusion_reason": "crawl4ai_source_error",
        **_page_context_fields(page_context),
        **extra,
    }


def _page_context_fields(page_context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the small, serialisable stage envelope for one page.

    The envelope is deliberately not a free-form browser metadata channel.
    It records only whether a page was a declared index or detail capture and
    the immutable parent evidence needed to audit why the detail URL was
    requested.
    """

    context = page_context if isinstance(page_context, Mapping) else {}
    stage = "detail" if context.get("crawl_stage") == "detail" else "index"
    fields: dict[str, Any] = {"crawl_stage": stage}
    for key in ("parent_url", "parent_content_sha256", "follow_reason"):
        value = context.get(key)
        if isinstance(value, str) and value:
            fields[key] = value[:2048]
    return fields


def _config_identity(config: Crawl4AIConfig) -> tuple[str, str, str]:
    """Build a deterministic identity for one declared page lane."""

    return (str(config.source_id or ""), config.url, config.parser)


def _archive_raw_page(
    raw: bytes,
    page: Mapping[str, Any],
    archive_dir: Path,
) -> dict[str, Any]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = archive_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    raw_path = raw_dir / f"{digest}.html"
    raw_lock_path = raw_dir / ".archive.lock"
    try:
        # Different host workers can finish with identical content at the
        # same time.  Lock the content-addressed write so a reader cannot see
        # the other worker's file between creation and fsync.
        with raw_lock_path.open("a+") as raw_lock:
            fcntl.flock(raw_lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    with raw_path.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    if hashlib.sha256(raw_path.read_bytes()).hexdigest() != digest:
                        raise Crawl4AIConfigError("existing Crawl4AI raw path has a different hash")
            finally:
                fcntl.flock(raw_lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise Crawl4AIConfigError(f"cannot archive Crawl4AI raw page: {raw_path}") from exc

    manifest = archive_dir / "pages.jsonl"
    lock_path = archive_dir / "pages.jsonl.lock"
    manifest_row = {
        "schema_version": "1.0.0",
        "source": page.get("source"),
        "source_id": page.get("source_id"),
        "fact_source_id": page.get("fact_source_id"),
        "capture_engine": page.get("capture_engine"),
        "capture_role": page.get("capture_role"),
        "url": page.get("url"),
        "final_url": page.get("final_url"),
        "observed_at": page.get("observed_at"),
        "effective_at": page.get("effective_at"),
        "time_fallback": page.get("time_fallback"),
        "status_code": page.get("status_code"),
        "content_sha256": digest,
        "content_size": len(raw),
        "raw_path": str(raw_path.relative_to(archive_dir)),
        "robots": page.get("robots"),
        "crawl_stage": page.get("crawl_stage"),
        "parent_url": page.get("parent_url"),
        "parent_content_sha256": page.get("parent_content_sha256"),
        "follow_reason": page.get("follow_reason"),
        "extraction": page.get("extraction"),
        "extracted_record_count": page.get("extraction", {}).get("record_count") if isinstance(page.get("extraction"), Mapping) else 0,
        "enters_model": False,
        "model_exclusion_reason": page.get("model_exclusion_reason"),
    }
    line = json.dumps(manifest_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            existing_hashes = set()
            if manifest.exists():
                with manifest.open(encoding="utf-8") as stream:
                    for previous in stream:
                        try:
                            value = json.loads(previous)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, Mapping) and value.get("content_sha256") == digest and value.get("url") == page.get("url"):
                            existing_hashes.add(digest)
            if digest not in existing_hashes:
                with manifest.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise Crawl4AIConfigError(f"cannot append Crawl4AI archive manifest: {manifest}") from exc
    return {"content_sha256": digest, "raw_sha256": digest, "raw_path": str(raw_path.relative_to(archive_dir)), "duplicate": bool(existing_hashes)}


def _build_page(
    config: Crawl4AIConfig,
    result: Any,
    *,
    observed_at: datetime,
    robots: Mapping[str, Any],
    archive_dir: Path | None,
    page_context: Mapping[str, Any] | None = None,
    rights_decision: source_rights.Decision | None = None,
    test_only_runner: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    success = bool(getattr(result, "success", False))
    if not success:
        return None, _page_error(
            config,
            observed_at=observed_at,
            stage="crawl",
            error=getattr(result, "error_message", None) or "Crawl4AI returned an unsuccessful result",
            status_code=getattr(result, "status_code", None),
            page_context=page_context,
        )
    raw_text = getattr(result, "html", None)
    raw_kind = "html"
    if not isinstance(raw_text, str) or not raw_text:
        raw_text = _markdown_value(result)
        raw_kind = "markdown_fallback"
    if not isinstance(raw_text, str) or not raw_text:
        return None, _page_error(
            config,
            observed_at=observed_at,
            stage="parse",
            error="Crawl4AI result contains no HTML or Markdown",
            page_context=page_context,
        )
    raw = raw_text.encode("utf-8")
    if len(raw) > config.max_bytes:
        return None, _page_error(
            config,
            observed_at=observed_at,
            stage="size",
            error=f"Crawl4AI result exceeded {config.max_bytes} bytes",
            content_size=len(raw),
            page_context=page_context,
        )
    final_url = getattr(result, "redirected_url", None) or getattr(result, "url", None) or config.url
    try:
        _validate_url(str(final_url), config)
    except Crawl4AIConfigError as exc:
        return None, _page_error(
            config,
            observed_at=observed_at,
            stage="redirect",
            error=str(exc),
            final_url=str(final_url),
            page_context=page_context,
        )
    effective_at, time_fallback = _metadata_date(result, observed_at)
    markdown, markdown_truncated = _safe_excerpt(_markdown_value(result))
    headers = _header_map(getattr(result, "response_headers", None))
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    structured_json: Any = None
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            parsed_json = json.loads(raw_text)
        except json.JSONDecodeError:
            return None, _page_error(
                config,
                observed_at=observed_at,
                stage="content_type",
                error="response declared JSON but could not be decoded",
                content_type=content_type,
                page_context=page_context,
            )
        if not isinstance(parsed_json, (Mapping, list)):
            return None, _page_error(
                config,
                observed_at=observed_at,
                stage="content_type",
                error="JSON response must be an object or array",
                content_type=content_type,
                page_context=page_context,
            )
        raw_kind = "json"
        structured_json = _json_safe(parsed_json)
        markdown = ""
        markdown_truncated = False
    elif content_type in {"text/html", "application/xhtml+xml"}:
        # A few public endpoints advertise an HTML page while returning a
        # JSON error/fixture body (and the inverse also occurs).  Do not let
        # a body/header mismatch look like a successful page capture: the
        # source-specific adapter must decide which contract is valid.
        stripped = raw_text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(raw_text)
            except json.JSONDecodeError:
                pass
            else:
                return None, _page_error(
                    config,
                    observed_at=observed_at,
                    stage="content_type",
                    error="response declared HTML but body decoded as JSON",
                    content_type=content_type,
                    page_context=page_context,
                )
    structured_facts = _structured_facts(raw_text) if raw_kind == "html" else []
    parser_name, extracted_records = (
        _source_specific_records(
            source_name=config.name,
            final_url=str(final_url),
            raw_html=raw_text,
            parser_contract=config.parser,
        )
        if raw_kind == "html"
        else (None, [])
    )
    # ``auto`` may resolve from the response host.  Re-run the identity gate
    # against the resolved parser and final URL before any page/archive record
    # is emitted; otherwise a page on a shared/redirected host could be
    # relabelled as the configured provider.
    # Validate the resolved final host even when ``auto`` has no parser for
    # it.  Otherwise an operator-broadened allowlist could redirect a
    # registered source to an unrelated host, emit a page with the original
    # source identity, and evade the parser/source/host isolation contract.
    mismatch = _parser_source_mismatch(
        config,
        parser_contract=parser_name or config.parser,
        url=str(final_url),
    )
    if mismatch is not None:
        return None, _parser_mismatch_error(
            config,
            mismatch,
            parser_contract=parser_name,
        )
    extraction = {
        "parser": parser_name,
        "parser_contract": config.parser,
        "record_count": len(extracted_records),
        "status": "ok" if extracted_records else "no_records",
        "model_admission": "blocked_display_only_until_fixture_join_license_review_and_freeze_proof",
    }
    metadata = _json_safe(getattr(result, "metadata", {}) or {})
    if not isinstance(metadata, dict):
        metadata = {}
    # Keep the bounded JSON-LD facts inside the already-ingested metadata
    # envelope as well as on the page record.  The ingestion lock only
    # forwards this envelope to the display ledger; it does not make these
    # facts model features.  This lets Sites show the evidence without
    # changing the frozen prospective model code or its hash.
    if structured_facts:
        metadata["structured_facts"] = structured_facts
        metadata["structured_facts_count"] = len(structured_facts)
    if parser_name:
        metadata["extraction"] = extraction
        metadata["extracted_record_count"] = len(extracted_records)
    rights_payload = rights_decision.as_dict() if rights_decision is not None else None
    page: dict[str, Any] = {
        "source": config.name,
        "source_id": config.source_id,
        "fact_source_id": config.source_id,
        "provider": "Crawl4AI",
        "provider_role": "execution_layer",
        "capture_engine": "Crawl4AI",
        "capture_role": "fetch_runtime",
        "source_role": "fact_source_capture",
        "source_tier": config.source_tier,
        "parser_contract": config.parser,
        "url": config.url,
        "final_url": str(final_url),
        "status_code": getattr(result, "status_code", None),
        "retrieved_at": observed_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "effective_at": effective_at,
        "time_fallback": time_fallback,
        "content_type": headers.get("content-type"),
        "raw_kind": raw_kind,
        "content_size": len(raw),
        "markdown": markdown,
        "markdown_truncated": markdown_truncated,
        "structured_facts": structured_facts,
        "structured_facts_count": len(structured_facts),
        "structured_json": structured_json,
        "extraction": extraction,
        "extracted_records": extracted_records,
        # Keep the scalar alongside the bounded record list. The list is
        # useful for the evidence ledger, while the scalar is the stable
        # projection consumed by source-health/read-model clients. Without
        # this field fresh pages looked different from restored stale pages.
        "extracted_record_count": len(extracted_records),
        "metadata": metadata,
        "robots": _json_safe(dict(robots)),
        "policy": {
            "robots_allowed": True,
            "robots_allowed_for_model": False,
            "model_eligible": False,
            "allow_model": False,
            "license_status": config.license_status,
            "rights_reference": config.rights_reference,
            "rights": rights_payload,
            "rights_status": (
                rights_decision.rights_status if rights_decision is not None else None
            ),
            "access_allowed": (
                rights_decision.access_allowed if rights_decision is not None else False
            ),
            "network_opened": False if test_only_runner else True,
            "runtime_evidence": not test_only_runner,
            "test_only": test_only_runner,
            "ai_train_allowed": None,
            "requires_login": False,
            "captcha_required": False,
        },
        "requested_model_eligible": bool(config.model_eligible),
        "model_eligible": False,
        "enters_model": False,
        "network_opened": False if test_only_runner else True,
        "runtime_evidence": not test_only_runner,
        "test_only": test_only_runner,
        "model_exclusion_reason": "unstructured_browser_capture_requires_source_parser",
        **_page_context_fields(page_context),
    }
    if archive_dir is not None:
        archive = _archive_raw_page(raw, page, archive_dir)
        page.update(archive)
    else:
        digest = hashlib.sha256(raw).hexdigest()
        page.update({"content_sha256": digest, "raw_sha256": digest})
    return page, None


def _config_from_mapping(value: Mapping[str, Any]) -> Crawl4AIConfig:
    allowed_hosts = value.get("allowed_hosts", ())
    if isinstance(allowed_hosts, str):
        allowed_hosts = (allowed_hosts,)
    path_prefixes = value.get("allowed_path_prefixes", ("/",))
    if isinstance(path_prefixes, str):
        path_prefixes = (path_prefixes,)
    follow_record_fields = value.get("follow_record_fields", ("match_url", "article_url"))
    if isinstance(follow_record_fields, str):
        follow_record_fields = (follow_record_fields,)
    source_id = value.get("source_id")
    follow_parser = value.get("follow_parser")
    if follow_parser is not None:
        follow_parser = str(follow_parser)
    follow_delay = value.get("follow_up_min_delay_seconds")
    if isinstance(follow_delay, bool):
        raise Crawl4AIConfigError("follow_up_min_delay_seconds must be numeric")
    return Crawl4AIConfig(
        name=str(value.get("name") or ""),
        url=str(value.get("url") or ""),
        source_id=str(source_id) if source_id is not None else None,
        parser=str(value.get("parser", "auto")),
        enabled=value.get("enabled", True),
        test_only=value.get("test_only", False),
        allowed_hosts=tuple(str(item) for item in allowed_hosts),
        allowed_path_prefixes=tuple(str(item) for item in path_prefixes),
        source_tier=str(value.get("source_tier", "reliable_public_provider")),
        model_eligible=bool(value.get("model_eligible", False)),
        license_status=str(value.get("license_status", "unknown")),
        rights_reference=(
            str(value["rights_reference"])
            if value.get("rights_reference") is not None
            else None
        ),
        check_robots=bool(value.get("check_robots", True)),
        timeout_seconds=float(value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        max_bytes=int(value.get("max_bytes", DEFAULT_MAX_BYTES)),
        min_delay_seconds=float(value.get("min_delay_seconds", DEFAULT_DELAY_SECONDS)),
        user_agent=str(value.get("user_agent", DEFAULT_USER_AGENT)),
        follow_links=value.get("follow_links", False),
        max_follow_up_pages=value.get("max_follow_up_pages", 0),
        follow_record_fields=tuple(str(item) for item in follow_record_fields),
        follow_parser=follow_parser,
        follow_up_min_delay_seconds=(float(follow_delay) if follow_delay is not None else None),
    )


def _bounded_config_value(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _rights_blocked_error(
    value: Mapping[str, Any],
    *,
    index: int,
    decision: source_rights.Decision | None = None,
    error_code: str = "source_rights_not_verified",
    error: str = "enabled source requires open or authorized rights before automated access",
) -> dict[str, Any]:
    """Keep a bounded, non-network diagnostic for one blocked declaration."""

    payload: dict[str, Any] = {
        "stage": "rights_gate",
        "status": "rights_blocked",
        "error_code": error_code,
        "index": index,
        "source_id": _bounded_config_value(value.get("source_id"), limit=100),
        "name": _bounded_config_value(value.get("name"), limit=200),
        "url": _bounded_config_value(value.get("url"), limit=2048),
        "enabled": value.get("enabled", True) is True,
        "license_status": _bounded_config_value(
            value.get("license_status", "unknown"), limit=40
        ),
        "rights_reference": _bounded_config_value(
            value.get("rights_reference"), limit=2048
        ),
        "network_opened": False,
        "model_eligible": False,
        "enters_model": False,
        "error": error,
    }
    if decision is not None:
        payload.update(
            {
                "rights": decision.as_dict(),
                "rights_status": decision.rights_status,
                "access_allowed": False,
                "commercial_reuse_verified": decision.commercial_reuse_verified,
            }
        )
    return payload


def _rights_blocked_sources(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for error in errors:
        if error.get("status") != "rights_blocked":
            continue
        row: dict[str, Any] = {
            "source_id": error.get("source_id"),
            "name": error.get("name"),
            "url": error.get("url"),
            "license_status": error.get("license_status"),
            "rights_reference": error.get("rights_reference"),
            "network_opened": False,
        }
        if "rights_status" in error:
            row["rights_status"] = error.get("rights_status")
        if "rights" in error and isinstance(error.get("rights"), Mapping):
            row["decision"] = error["rights"].get("decision")
        if "model_eligible" in error:
            row["model_eligible"] = False
        rows.append(row)
    return rows


def _rights_gate_error_for_config(
    config: Crawl4AIConfig,
    decision: source_rights.Decision,
    *,
    reason: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a structured pre-network block for an in-memory config."""

    future_disabled = decision.decision is source_rights.DecisionValue.FUTURE_DISABLED
    return {
        "stage": "rights_gate",
        "status": "rights_blocked",
        "error_code": error_code
        or ("source_rights_future_disabled" if future_disabled else "source_rights_not_verified"),
        "source": config.name,
        "source_id": config.source_id,
        "fact_source_id": config.source_id,
        "url": config.url,
        "parser_contract": config.parser,
        "license_status": config.license_status,
        "rights_reference": config.rights_reference,
        "rights": decision.as_dict(),
        "rights_status": decision.rights_status,
        "access_allowed": False,
        "commercial_reuse_verified": decision.commercial_reuse_verified,
        "network_opened": False,
        "model_eligible": False,
        "enters_model": False,
        "runtime_evidence": False,
        "test_only": bool(config.test_only),
        "error": reason
        or (
            "source is future-disabled by the central v260 policy"
            if future_disabled
            else "source rights are blocked by the central v260 policy"
        ),
    }


def _parser_mismatch_error(
    config: Crawl4AIConfig,
    reason: str,
    *,
    parser_contract: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Build a structured pre-network parser/source admission failure."""

    payload: dict[str, Any] = {
        "stage": "admission",
        "status": "unavailable",
        "error_code": "parser_source_mismatch",
        "source": config.name,
        "source_id": config.source_id,
        "fact_source_id": config.source_id,
        "url": config.url,
        "parser_contract": config.parser,
        "network_opened": False,
        "model_eligible": False,
        "enters_model": False,
        "runtime_evidence": False,
        "test_only": bool(config.test_only),
        "error": reason,
    }
    if parser_contract is not None:
        payload["resolved_parser_contract"] = parser_contract
    if index is not None:
        payload["index"] = index
    return payload


def _custom_runner_error(config: Crawl4AIConfig) -> dict[str, Any]:
    """Build the fail-closed diagnostic for a production custom runner."""

    return {
        "stage": "runner_policy",
        "status": "unavailable",
        "error_code": "custom_runner_forbidden",
        "source": config.name,
        "source_id": config.source_id,
        "fact_source_id": config.source_id,
        "url": config.url,
        "network_opened": False,
        "model_eligible": False,
        "enters_model": False,
        "runtime_evidence": False,
        "test_only": bool(config.test_only),
        "error": "custom Crawl4AI runner is permitted only for an explicit test_only config",
    }


def _admission_preflight(
    configs: Sequence[Crawl4AIConfig],
    *,
    test_only_runner: bool,
    observed_at: datetime,
) -> tuple[
    dict[int, source_rights.Decision],
    dict[int, dict[str, Any]],
]:
    """Evaluate every in-memory lane before any runtime inspection or I/O.

    Config-file rows already pass through :func:`load_crawl4ai_configs`, but
    callers may provide ``Crawl4AIConfig`` objects directly.  Keeping this
    serial preflight at the public boundary gives those callers the same
    ordering guarantee: all central rights decisions (and the dependent
    parser/runner admission checks) finish before TLS/browser inspection,
    robots requests, or the cross-host worker pool starts.  Results are keyed
    by object identity so two otherwise-identical declarations cannot share a
    decision accidentally.
    """

    decisions: dict[int, source_rights.Decision] = {}
    errors: dict[int, dict[str, Any]] = {}
    for config in configs:
        if not isinstance(config, Crawl4AIConfig) or not config.enabled:
            continue
        key = id(config)
        try:
            decision = _central_rights_decision(config)
        except Exception as exc:  # an unavailable policy is fail-closed
            errors[key] = _page_error(
                config,
                observed_at=observed_at,
                stage="rights_gate",
                error=f"central source-rights decision failed: {exc}",
                error_code="source_rights_unavailable",
            )
            continue

        # Do not trust a monkeypatched/serialized decision merely because it
        # exposes an ``is_allowed`` attribute.  The policy result must be the
        # typed network decision for this exact source and use case; a result
        # for another source could otherwise uplift a blocked declaration.
        decision_source = getattr(decision, "source_id", None)
        decision_use_case = getattr(decision, "use_case", None)
        expected_source = str(config.source_id or "")
        expected_use_case = source_rights.UseCase.NETWORK_FETCH.value
        if (
            not isinstance(decision, source_rights.Decision)
            or decision_source != expected_source
            or decision_use_case != expected_use_case
        ):
            errors[key] = _rights_gate_error_for_config(
                config,
                decision if isinstance(decision, source_rights.Decision) else None,
                error_code="source_rights_identity_mismatch",
                reason=(
                    "central source-rights decision identity does not match the "
                    "declared source/network use case"
                ),
            )
            continue
        if not decision.is_allowed:
            future_disabled = decision.decision is source_rights.DecisionValue.FUTURE_DISABLED
            errors[key] = _rights_gate_error_for_config(
                config,
                decision,
                error_code=(
                    "source_rights_future_disabled"
                    if future_disabled
                    else "source_rights_not_verified"
                ),
            )
            continue
        if not _config_rights_declaration_allowed(config):
            errors[key] = _rights_gate_error_for_config(
                config,
                decision,
                error_code="source_rights_declaration_unverified",
                reason=(
                    "operator license_status/rights_reference is incomplete; "
                    "central policy ALLOW cannot be replaced by a self-attestation"
                ),
            )
            continue
        mismatch = _parser_source_mismatch(config)
        if mismatch is not None:
            errors[key] = _parser_mismatch_error(config, mismatch)
            continue
        if test_only_runner and not config.test_only:
            errors[key] = _custom_runner_error(config)
            continue
        decisions[key] = decision
    return decisions, errors


def load_crawl4ai_configs(
    *,
    raw: str | bytes | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    config_path: Path | str | None = None,
) -> tuple[list[Crawl4AIConfig], list[dict[str, Any]]]:
    """Load explicit source configuration without silently enabling a URL."""

    if raw is None and config_path is None:
        raw = os.environ.get("MATCHLINE_CRAWL4AI_SOURCES")
        if raw is None:
            config_path = os.environ.get("MATCHLINE_CRAWL4AI_CONFIG")
    if raw is None and config_path:
        try:
            raw = Path(config_path).read_text(encoding="utf-8")
        except OSError as exc:
            return [], [{"stage": "config", "error": str(exc)}]
    if raw is None or raw == "":
        return [], []
    try:
        if isinstance(raw, (str, bytes)):
            parsed = json.loads(raw)
        else:
            parsed = raw
    except (TypeError, json.JSONDecodeError) as exc:
        return [], [{"stage": "config", "error": f"invalid JSON: {exc}"}]
    if isinstance(parsed, Mapping):
        parsed = parsed.get("sources", parsed.get("crawl4ai", []))
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
        return [], [{"stage": "config", "error": "sources must be a JSON array"}]
    configs: list[Crawl4AIConfig] = []
    errors: list[dict[str, Any]] = []
    for index, value in enumerate(parsed):
        if not isinstance(value, Mapping):
            errors.append({"stage": "config", "index": index, "error": "source entry must be an object"})
            continue
        try:
            config = _config_from_mapping(value)
        except _Crawl4AIRightsBlocked:
            errors.append(_rights_blocked_error(value, index=index))
        except (Crawl4AIConfigError, TypeError, ValueError) as exc:
            errors.append({"stage": "config", "index": index, "error": str(exc)})
            continue

        if not config.enabled:
            continue

        # Consult the central policy after syntax/allowlist validation but
        # before any robots check, browser launch, or network request.  The
        # operator's ``license_status``/``rights_reference`` remain untrusted
        # context and cannot uplift a v260 BLOCK/FUTURE_DISABLED decision.
        decision = _central_rights_decision(config)
        if (
            not isinstance(decision, source_rights.Decision)
            or decision.source_id != str(config.source_id or "")
            or decision.use_case != source_rights.UseCase.NETWORK_FETCH.value
        ):
            errors.append(
                _rights_gate_error_for_config(
                    config,
                    decision if isinstance(decision, source_rights.Decision) else None,
                    error_code="source_rights_identity_mismatch",
                    reason=(
                        "central source-rights decision identity does not match the "
                        "declared source/network use case"
                    ),
                )
            )
            continue
        if not decision.is_allowed:
            code = (
                "source_rights_future_disabled"
                if decision.decision is source_rights.DecisionValue.FUTURE_DISABLED
                else "source_rights_not_verified"
            )
            errors.append(
                _rights_blocked_error(
                    value,
                    index=index,
                    decision=decision,
                    error_code=code,
                    error=(
                        "source is future-disabled by the central v260 policy"
                        if decision.decision is source_rights.DecisionValue.FUTURE_DISABLED
                        else "source rights are blocked by the central v260 policy"
                    ),
                )
            )
            continue
        if not _config_rights_declaration_allowed(config):
            errors.append(
                _rights_blocked_error(
                    value,
                    index=index,
                    decision=decision,
                    error_code="source_rights_declaration_unverified",
                    error=(
                        "operator license_status/rights_reference is incomplete; "
                        "central policy ALLOW cannot be replaced by a self-attestation"
                    ),
                )
            )
            continue
        mismatch = _parser_source_mismatch(config)
        if mismatch is not None:
            errors.append(
                _parser_mismatch_error(
                    config,
                    mismatch,
                    index=index,
                )
            )
            continue
        configs.append(config)
    return configs, errors


def _load_throttle_state(archive_dir: Path | None) -> dict[str, str]:
    """Read the last-request ledger used across short-lived systemd runs."""

    if archive_dir is None:
        return {}
    path = archive_dir / THROTTLE_STATE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    state: dict[str, str] = {}
    for host, timestamp in value.items():
        if isinstance(host, str) and host and isinstance(timestamp, str) and timestamp:
            state[host] = timestamp
    return state


def _persist_throttle_state(archive_dir: Path | None, state: Mapping[str, str]) -> None:
    """Atomically append the bounded throttle state without touching page history."""

    if archive_dir is None:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / THROTTLE_STATE_FILENAME
    temporary = archive_dir / f".{THROTTLE_STATE_FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        temporary.write_text(
            json.dumps(
                {str(host): str(timestamp) for host, timestamp in list(state.items())[:100]},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        # A throttle ledger is operational metadata, not a source fact.  Do
        # not fail a whole cycle merely because the optional state file could
        # not be refreshed; the in-process guard still remains active.


def _page_cache_key(
    url: str,
    *,
    source_id: str | None = None,
    parser_contract: str | None = None,
) -> str:
    """Build a stale-cache key that cannot relabel one source as another."""

    identity = "|".join((url, source_id or "", parser_contract or ""))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _follow_up_plan(
    pages: Sequence[Mapping[str, Any]],
    configs: Sequence[Crawl4AIConfig],
) -> tuple[
    list[Crawl4AIConfig],
    dict[tuple[str, str, str], dict[str, Any]],
    dict[str, int],
    list[dict[str, Any]],
]:
    """Build a bounded detail-page plan from source-parser records.

    No DOM links are inspected here.  A URL is eligible only when it was
    emitted by a configured parser field (``match_url`` or ``article_url``),
    remains HTTPS, stays on an explicitly allowlisted host/path, and belongs
    to a source with ``follow_links=true``.  Invalid candidates are returned
    as bounded audit rows rather than silently treated as successful work.
    """

    config_by_identity = {_config_identity(config): config for config in configs}
    follow_configs: list[Crawl4AIConfig] = []
    contexts: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    metrics = {
        "follow_up_candidate_count": 0,
        "follow_up_requested_count": 0,
        "follow_up_rejected_count": 0,
        "follow_up_capped_count": 0,
    }
    rejections: list[dict[str, Any]] = []
    requested_by_source: dict[str, int] = {}

    def reject(config: Crawl4AIConfig, value: Any, reason: str) -> None:
        metrics["follow_up_rejected_count"] += 1
        if len(rejections) >= 50:
            return
        raw_value = str(value)
        parsed_value = urlparse(raw_value)
        if parsed_value.scheme or parsed_value.netloc:
            try:
                host = parsed_value.hostname or ""
                port = parsed_value.port
            except ValueError:
                host, port = "", None
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            safe_netloc = host + (f":{port}" if port is not None else "")
            safe_value = parsed_value._replace(
                netloc=safe_netloc,
                query="",
                fragment="",
            ).geturl()
        else:
            # Relative candidates can still contain tracking tokens. Keep
            # the path for audit, but never echo a query or fragment into the
            # public diagnostic payload.
            safe_value = parsed_value._replace(query="", fragment="").geturl()
        rejections.append(
            {
                "source_id": config.source_id,
                "source": config.name,
                "value": safe_value[:2048],
                "reason": reason,
                "enters_model": False,
            }
        )

    for page in pages:
        if not isinstance(page, Mapping) or page.get("stale") is True:
            continue
        source_id = str(page.get("source_id") or "")
        source_url = page.get("url")
        parser_contract = str(page.get("parser_contract") or "")
        if not isinstance(source_url, str):
            continue
        config = config_by_identity.get((source_id, source_url, parser_contract))
        if config is None or not config.follow_links or config.max_follow_up_pages <= 0:
            continue
        records = page.get("extracted_records")
        if not isinstance(records, list):
            continue
        source_key = source_id or config.name
        requested_for_source = requested_by_source.get(source_key, 0)
        parent_url = str(page.get("final_url") or source_url)
        parent_hash = str(page.get("raw_sha256") or page.get("content_sha256") or "")
        for record in records:
            if not isinstance(record, Mapping):
                continue
            for field in config.follow_record_fields:
                if field not in record:
                    continue
                raw_candidate = record.get(field)
                if not isinstance(raw_candidate, str) or not raw_candidate.strip():
                    continue
                metrics["follow_up_candidate_count"] += 1
                candidate = urljoin(parent_url, raw_candidate.strip())
                parsed = urlparse(candidate)
                candidate = parsed._replace(fragment="").geturl()
                try:
                    _validate_url(candidate, config)
                except Crawl4AIConfigError as exc:
                    reject(config, raw_candidate, str(exc))
                    continue
                if candidate.rstrip("/") == parent_url.rstrip("/"):
                    reject(config, raw_candidate, "follow-up URL resolves to its parent page")
                    continue
                follow_parser = config.follow_parser or config.parser
                follow_identity = (source_id, candidate, follow_parser)
                if follow_identity in seen:
                    continue
                seen.add(follow_identity)
                if requested_for_source >= config.max_follow_up_pages:
                    metrics["follow_up_capped_count"] += 1
                    continue
                requested_for_source += 1
                requested_by_source[source_key] = requested_for_source
                detail_config = replace(
                    config,
                    url=candidate,
                    parser=follow_parser,
                    follow_links=False,
                    max_follow_up_pages=0,
                    follow_record_fields=(),
                    follow_parser=None,
                    follow_up_min_delay_seconds=None,
                    min_delay_seconds=(
                        config.follow_up_min_delay_seconds
                        if config.follow_up_min_delay_seconds is not None
                        else config.min_delay_seconds
                    ),
                )
                follow_configs.append(detail_config)
                contexts[_config_identity(detail_config)] = {
                    "crawl_stage": "detail",
                    "parent_url": parent_url,
                    "parent_content_sha256": parent_hash,
                    "follow_reason": field,
                }
                metrics["follow_up_requested_count"] += 1
    return follow_configs, contexts, metrics, rejections


def _load_latest_pages(archive_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Read the bounded last-success cache used only for stale display evidence.

    ``pages.jsonl`` and the content-addressed raw files remain the source of
    truth.  This small mutable index is only an operational pointer so a
    throttled poll can keep showing the last verified page with an explicit
    stale marker instead of replacing the evidence panel with an empty list.
    """

    if archive_dir is None:
        return {}
    path = archive_dir / LATEST_PAGES_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    entries = value.get("pages") if isinstance(value, Mapping) else None
    if not isinstance(entries, Mapping):
        return {}
    return {
        str(key): dict(page)
        for key, page in entries.items()
        if isinstance(key, str) and isinstance(page, Mapping)
    }


def _persist_latest_page(archive_dir: Path | None, page: Mapping[str, Any]) -> None:
    """Atomically point the operational stale cache at one verified page."""

    if archive_dir is None or not isinstance(page.get("url"), str):
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    lock_path = archive_dir / LATEST_PAGES_LOCK_FILENAME
    path = archive_dir / LATEST_PAGES_FILENAME
    temporary = archive_dir / f".{LATEST_PAGES_FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            entries = _load_latest_pages(archive_dir)
            entries[
                _page_cache_key(
                    str(page["url"]),
                    source_id=str(page.get("source_id") or ""),
                    parser_contract=str(page.get("parser_contract") or ""),
                )
            ] = dict(page)
            ordered = sorted(
                entries.items(),
                key=lambda item: str(item[1].get("observed_at") or ""),
            )[-100:]
            temporary.write_text(
                json.dumps(
                    {"schema_version": "1.0.0", "pages": dict(ordered)},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        temporary.unlink(missing_ok=True)
        # The cache is derived operational metadata.  A failed cache update
        # must not turn an otherwise valid source page into a failed cycle.


def _latest_cached_page_for_config(
    latest_pages: Mapping[str, Mapping[str, Any]], config: Crawl4AIConfig
) -> Mapping[str, Any] | None:
    """Find a stale pointer even when a source parser was just upgraded.

    The exact cache key remains the fast path. If a page was archived under
    the generic contract before a narrow source parser was added, the raw
    page can be re-projected in memory; it never receives a fresh timestamp
    or model admission.
    """

    exact = latest_pages.get(
        _page_cache_key(
            config.url,
            source_id=config.source_id,
            parser_contract=config.parser,
        )
    )
    if isinstance(exact, Mapping):
        return exact
    legacy = latest_pages.get(_page_cache_key(config.url))
    if isinstance(legacy, Mapping):
        return legacy
    candidates = [
        page
        for page in latest_pages.values()
        if isinstance(page, Mapping)
        and page.get("url") == config.url
        and page.get("source_id") == config.source_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda page: str(page.get("observed_at") or ""))


def _restore_stale_page(
    config: Crawl4AIConfig,
    page: Mapping[str, Any] | None,
    *,
    archive_dir: Path | None,
    reference_time: datetime,
    reason: str,
) -> dict[str, Any] | None:
    """Return a verified last-success page marked stale, or ``None``.

    The raw bytes and content hash are checked again before reuse.  A stale
    page never receives the current observation time and is always forced out
    of model use; this is a display continuity measure, not a freshness claim.
    """

    if archive_dir is None or not isinstance(page, Mapping) or page.get("url") != config.url:
        return None
    # The cache is keyed by URL for operational convenience, but a URL is
    # not a fact-source identity.  When an operator replaces or renames a
    # source configuration, never relabel the old page as the new source (and
    # never reuse a page parsed under a different contract).  Failing closed
    # here is preferable to showing a plausible-looking stale card under the
    # wrong provider.
    if page.get("source_id") != config.source_id:
        return None
    cached_parser = page.get("parser_contract")
    parser_reprojection = (
        cached_parser != config.parser
        and cached_parser in {None, "generic_public_page_v1"}
        and config.parser == "football_data_archive_v1"
    )
    if cached_parser is not None and cached_parser != config.parser and not parser_reprojection:
        return None
    raw_relative = page.get("raw_path")
    if not isinstance(raw_relative, str) or not raw_relative or Path(raw_relative).is_absolute():
        return None
    raw_path = archive_dir / raw_relative
    try:
        raw_path.resolve().relative_to(archive_dir.resolve())
        size = raw_path.stat().st_size
        if size <= 0 or size > config.max_bytes:
            return None
        raw = raw_path.read_bytes()
    except OSError:
        return None
    expected_hash = page.get("raw_sha256") or page.get("content_sha256")
    if not isinstance(expected_hash, str) or hashlib.sha256(raw).hexdigest() != expected_hash:
        return None
    try:
        observed = datetime.fromisoformat(str(page.get("observed_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None or observed.utcoffset() is None:
        return None
    if observed.astimezone(timezone.utc) > reference_time.astimezone(timezone.utc):
        return None
    try:
        _validate_url(str(page.get("final_url") or page.get("url")), config)
    except Crawl4AIConfigError:
        return None
    extracted_records = page.get("extracted_records")
    extraction = page.get("extraction")
    if parser_reprojection:
        try:
            parser_name, reparsed_records = _source_specific_records(
                source_name=config.name,
                final_url=str(page.get("final_url") or page.get("url")),
                raw_html=raw.decode("utf-8", errors="replace"),
                parser_contract=config.parser,
            )
        except Exception:
            return None
        if parser_name != config.parser:
            return None
        extracted_records = reparsed_records
        extraction = {
            **(dict(extraction) if isinstance(extraction, Mapping) else {}),
            "parser": config.parser,
            "parser_contract": config.parser,
            "record_count": len(reparsed_records),
            "status": "ok" if reparsed_records else "no_records",
            "model_admission": "blocked_display_only_until_fixture_join_license_review_and_freeze_proof",
        }
    restored = dict(page)
    restored.update(
        {
            # Older cached pages may predate the explicit engine/source
            # distinction.  Re-attach the runtime provenance when a page is
            # reused for display so a stale page cannot make Crawl4AI look
            # like an unnamed fact provider.
            "capture_engine": "Crawl4AI",
            "capture_role": "fetch_runtime",
            "source_role": "fact_source_capture",
            "source": config.name,
            "source_id": config.source_id,
            "fact_source_id": config.source_id,
            "parser_contract": config.parser,
            "extraction": extraction,
            "extracted_records": extracted_records,
            "extracted_record_count": len(extracted_records) if isinstance(extracted_records, list) else 0,
            "parser_reprojection": (
                {
                    "from": cached_parser or "unspecified",
                    "to": config.parser,
                    "reason": "stale_raw_page_reparsed_with_current_source_contract",
                }
                if parser_reprojection
                else None
            ),
            "stale": True,
            "fresh": False,
            "source_state": "stale",
            "stale_reason": reason,
            "stale_checked_at": reference_time.astimezone(timezone.utc).isoformat(),
            "enters_model": False,
            "model_eligible": False,
            "network_opened": False,
            "runtime_evidence": not config.test_only,
            "test_only": bool(config.test_only),
            "policy": {
                **(
                    dict(page.get("policy") or {})
                    if isinstance(page.get("policy"), Mapping)
                    else {}
                ),
                "model_eligible": False,
                "allow_model": False,
                "network_opened": False,
                "runtime_evidence": not config.test_only,
                "test_only": bool(config.test_only),
            },
        }
    )
    return restored


def _throttle_wait_seconds(previous: str | None, *, reference_time: datetime, minimum: float) -> float:
    if not previous or minimum <= 0:
        return 0.0
    try:
        parsed = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return 0.0
    elapsed = (reference_time.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    return max(0.0, float(minimum) - elapsed)


def _crawl_many(
    configs: Sequence[Crawl4AIConfig],
    *,
    reference_time: datetime,
    archive_dir: Path | None,
    robots_checker: Callable[..., Any] | None,
    runner: Callable[[Crawl4AIConfig], Any],
    sleep_fn: Callable[[float], Any],
    max_host_workers: int = DEFAULT_MAX_HOST_WORKERS,
    page_context_by_config: Mapping[tuple[str, str, str], Mapping[str, Any]] | None = None,
    test_only_runner: bool = False,
    admission_decisions: Mapping[int, source_rights.Decision] | None = None,
    admission_errors: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Crawl declared pages with cross-host parallelism and per-host serialism.

    A large source fan-out should not wait for the slowest unrelated host, but
    two pages on the same host must still obey the configured delay and share
    the same response cache.  Grouping by host gives us both properties while
    keeping output order deterministic and isolating a source-level failure.
    The worker count is deliberately bounded; it is an operational latency
    control, not a way to increase request pressure against one provider.
    """

    if isinstance(max_host_workers, bool) or not isinstance(max_host_workers, int):
        max_host_workers = DEFAULT_MAX_HOST_WORKERS
    worker_limit = max(1, min(max_host_workers, MAX_HOST_WORKERS))
    persisted_by_host = _load_throttle_state(archive_dir)
    latest_pages = _load_latest_pages(archive_dir)
    page_context_by_config = page_context_by_config or {}
    admission_decisions = admission_decisions or {}
    admission_errors = admission_errors or {}

    def page_context(config: Crawl4AIConfig) -> Mapping[str, Any] | None:
        return page_context_by_config.get(_config_identity(config))

    throttle_lock = threading.Lock()
    grouped: dict[str, list[Crawl4AIConfig]] = {}
    for index, config in enumerate(configs):
        # Keep malformed URLs in a deterministic group so the allowlist
        # diagnostic is still returned per source rather than aborting the
        # whole fan-out before validation.
        host = str(urlparse(config.url).hostname or f"<invalid-{index}>").lower()
        grouped.setdefault(host, []).append(config)

    def crawl_host(host: str, host_configs: Sequence[Crawl4AIConfig]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        host_pages: list[dict[str, Any]] = []
        host_errors: list[dict[str, Any]] = []
        last_request_at: float | None = None
        fetched_results_by_url: dict[str, tuple[Any, Mapping[str, Any]]] = {}
        for config in host_configs:
            observed_at = reference_time
            if not config.enabled:
                # ``load_crawl4ai_configs`` already filters disabled rows,
                # but callers may supply in-memory configs directly.  A
                # disabled declaration is never admitted to the central gate
                # or any downstream I/O path.
                continue
            preflight_error = admission_errors.get(id(config))
            if preflight_error is not None:
                # The public boundary has already evaluated this lane in a
                # serial, no-I/O preflight.  Preserve the original diagnostic
                # and do not run any of the checks below a second time.
                host_errors.append(dict(preflight_error))
                continue
            rights_decision = admission_decisions.get(id(config))
            if rights_decision is None:
                # Private callers may invoke _crawl_many directly.  Retain a
                # defensive gate for that path, while fetch_crawl4ai passes
                # the serial preflight maps above so no source can reach this
                # branch before all peers have been admitted.
                # This is intentionally the first operation for every enabled
                # source.  No URL validation, throttle bookkeeping, robots
                # request, or browser invocation may happen before the central
                # v260 policy has rendered its decision.
                rights_decision = _central_rights_decision(config)
                if not isinstance(rights_decision, source_rights.Decision):
                    host_errors.append(
                        _page_error(
                            config,
                            observed_at=observed_at,
                            stage="rights_gate",
                            error="central source-rights decision is not a typed Decision",
                            error_code="source_rights_identity_mismatch",
                            page_context=page_context(config),
                        )
                    )
                    continue
                if (
                    rights_decision.source_id != str(config.source_id or "")
                    or rights_decision.use_case != source_rights.UseCase.NETWORK_FETCH.value
                ):
                    host_errors.append(
                        _rights_gate_error_for_config(
                            config,
                            rights_decision,
                            error_code="source_rights_identity_mismatch",
                            reason=(
                                "central source-rights decision identity does not match the "
                                "declared source/network use case"
                            ),
                        )
                    )
                    continue
                if not rights_decision.is_allowed:
                    host_errors.append(_rights_gate_error_for_config(config, rights_decision))
                    continue
                if not _config_rights_declaration_allowed(config):
                    host_errors.append(
                        _rights_gate_error_for_config(
                            config,
                            rights_decision,
                            error_code="source_rights_declaration_unverified",
                            reason=(
                                "operator license_status/rights_reference is incomplete; "
                                "central policy ALLOW cannot be replaced by a self-attestation"
                            ),
                        )
                    )
                    continue
                mismatch = _parser_source_mismatch(config)
                if mismatch is not None:
                    host_errors.append(_parser_mismatch_error(config, mismatch))
                    continue
                if test_only_runner and not config.test_only:
                    host_errors.append(_custom_runner_error(config))
                    continue
            try:
                _validate_url(config.url, config)
            except Crawl4AIConfigError as exc:
                host_errors.append(
                    _page_error(
                        config,
                        observed_at=observed_at,
                        stage="allowlist",
                        error=str(exc),
                        page_context=page_context(config),
                    )
                )
                continue
            # A single source page may legitimately have more than one
            # declared parser identity (for example LaLiga's match and news
            # projections). Reuse the already fetched response in this host
            # worker instead of issuing duplicate same-host requests.
            cached_result = fetched_results_by_url.get(config.url)
            if cached_result is not None:
                result, robots = cached_result
                try:
                    page, error = _build_page(
                        config,
                        result,
                        observed_at=observed_at,
                        robots=robots,
                        archive_dir=archive_dir,
                        page_context=page_context(config),
                        rights_decision=rights_decision,
                        test_only_runner=test_only_runner,
                    )
                except Exception as exc:  # source-level isolation is part of the contract
                    page, error = None, _page_error(
                        config,
                        observed_at=observed_at,
                        stage="parse",
                        error=str(exc),
                        page_context=page_context(config),
                    )
                if page is not None:
                    host_pages.append(page)
                    _persist_latest_page(archive_dir, page)
                if error is not None:
                    host_errors.append(error)
                continue

            # Persisted throttle state is shared by host workers, so read and
            # update it under one lock. Only this host's worker can mutate the
            # monotonic guard, which preserves same-host serialization.
            if last_request_at is None:
                with throttle_lock:
                    persisted_wait = _throttle_wait_seconds(
                        persisted_by_host.get(host),
                        reference_time=reference_time,
                        minimum=config.min_delay_seconds,
                    )
                if persisted_wait > 0:
                    context = dict(page_context(config) or {})
                    if context.get("crawl_stage") == "detail":
                        # Index and detail pages intentionally share one host
                        # throttle. A detail fan-out in the same cycle must
                        # wait for that persisted delay instead of being
                        # classified as a new-process deferral; otherwise
                        # follow-up pages can never be fetched after an index
                        # page has just used the host budget. New processes
                        # still fail closed below and may reuse only verified
                        # stale evidence.
                        sleep_fn(persisted_wait)
                    else:
                        host_errors.append(
                            _page_error(
                                config,
                                observed_at=observed_at,
                                stage="throttle",
                                error=f"host is not due for another request ({persisted_wait:.0f}s remaining)",
                                error_code="rate_limited_wait",
                                retry_after_seconds=math.ceil(persisted_wait),
                                last_request_at=persisted_by_host.get(host),
                                page_context=context,
                            )
                        )
                        stale_page = _restore_stale_page(
                            config,
                            _latest_cached_page_for_config(latest_pages, config),
                            archive_dir=archive_dir,
                            reference_time=reference_time,
                            reason="host_throttled",
                        )
                        if stale_page is not None:
                            host_pages.append(stale_page)
                        continue
            else:
                wait = config.min_delay_seconds - (time.monotonic() - last_request_at)
                if wait > 0:
                    sleep_fn(wait)
            last_request_at = time.monotonic()
            with throttle_lock:
                persisted_by_host[host] = observed_at.astimezone(timezone.utc).isoformat()
                _persist_throttle_state(archive_dir, persisted_by_host)
            robots = _robots_result(config, robots_checker)
            if not robots.get("allowed"):
                host_errors.append(
                    _page_error(
                        config,
                        observed_at=observed_at,
                        stage="robots",
                        error="robots policy denied or could not be verified",
                        robots=robots,
                        page_context=page_context(config),
                    )
                )
                continue
            try:
                result = _run_async(
                    _invoke_runner_with_deadline(
                        runner,
                        config,
                        timeout=_runner_timeout_seconds(config),
                    )
                )
                fetched_results_by_url[config.url] = (result, robots)
                page, error = _build_page(
                    config,
                    result,
                    observed_at=observed_at,
                    robots=robots,
                    archive_dir=archive_dir,
                    page_context=page_context(config),
                    rights_decision=rights_decision,
                    test_only_runner=test_only_runner,
                )
            except _RunnerDeadlineExceeded:
                timeout_seconds = _runner_timeout_seconds(config)
                page, error = None, _page_error(
                    config,
                    observed_at=observed_at,
                    stage="crawl",
                    error=f"Crawl4AI runner exceeded {timeout_seconds:.1f}s bounded deadline",
                    error_code="timeout",
                    page_context=page_context(config),
                )
            except Exception as exc:  # source-level isolation is part of the contract
                page, error = None, _page_error(
                    config,
                    observed_at=observed_at,
                    stage="crawl",
                    error=str(exc),
                    page_context=page_context(config),
                )
            if page is not None:
                host_pages.append(page)
                _persist_latest_page(archive_dir, page)
            if error is not None:
                host_errors.append(error)
        return host_pages, host_errors

    def safe_crawl_host(host: str, host_configs: Sequence[Crawl4AIConfig]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            return crawl_host(host, host_configs)
        except Exception as exc:  # one host must never abort unrelated hosts
            return [], [
                _page_error(
                    config,
                    observed_at=reference_time,
                    stage="worker",
                    error=str(exc),
                    page_context=page_context(config),
                )
                for config in host_configs
            ]

    # Keep latency accounting independent from the per-host throttle clock;
    # the latter is intentionally injectable in tests and must retain its
    # first-request semantics.
    started = time.perf_counter()
    host_items = list(grouped.items())
    worker_count = min(worker_limit, len(host_items))
    if worker_count == 1:
        grouped_results = [safe_crawl_host(host, host_configs) for host, host_configs in host_items]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="matchline-crawl") as executor:
            futures = [executor.submit(safe_crawl_host, host, host_configs) for host, host_configs in host_items]
            # Resolve in declaration/host order, not completion order, so the
            # append-only snapshot and tests remain deterministic.
            grouped_results = [future.result() for future in futures]
    pages = [page for host_pages, _ in grouped_results for page in host_pages]
    errors = [error for _, host_errors in grouped_results for error in host_errors]
    throttle_only = bool(errors) and all(error.get("stage") == "throttle" for error in errors)
    fresh_page_count = sum(1 for page in pages if not page.get("stale"))
    stale_page_count = sum(1 for page in pages if page.get("stale") is True)
    if fresh_page_count:
        # A page that was fetched successfully plus another host that is
        # intentionally deferred is still a healthy cycle.  Preserve the
        # throttle rows for audit without turning scheduling into an outage.
        status = "ok" if not errors or throttle_only else "degraded"
    elif stale_page_count:
        # Keep the top-level scheduling state explicit.  The page itself is
        # still returned for the evidence panel, while source health below can
        # distinguish an all-stale view from a fresh successful page.
        status = "not_due" if throttle_only else "unavailable"
    else:
        if errors and all(error.get("status") == "rights_blocked" for error in errors):
            status = "rights_blocked"
        else:
            status = "not_due" if errors and all(error.get("stage") == "throttle" for error in errors) else "unavailable" if errors else "not_configured"
    runtime_evidence = bool(
        not test_only_runner
        and any(page.get("runtime_evidence") is not False for page in pages)
    )
    network_opened = bool(
        not test_only_runner
        and any(page.get("network_opened") is True for page in pages)
    )
    return {
        "provider": "Crawl4AI",
        "provider_role": "execution_layer",
        "retrieved_at": reference_time.isoformat(),
        "configured_count": len(configs),
        "page_count": len(pages),
        "fresh_page_count": fresh_page_count,
        "stale_page_count": stale_page_count,
        "pages": pages,
        "errors": errors,
        "status": status,
        "network_opened": network_opened,
        "model_eligible": False,
        "runtime_evidence": runtime_evidence,
        "runner_mode": "test_only" if test_only_runner else "production",
        "model_use": "display_only_unstructured_until_source_parser",
        "security_policy": _security_policy(),
        "execution": {
            "strategy": "bounded_cross_host_parallel_per_host_serial",
            "host_group_count": len(host_items),
            "max_host_workers": worker_count,
            "configured_source_count": len(configs),
            "test_only": bool(test_only_runner),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        },
        **_execution_layer_metadata(configs, pages, errors),
    }


def fetch_crawl4ai(
    *,
    now: datetime | None = None,
    configs: Sequence[Crawl4AIConfig] | None = None,
    config_path: Path | str | None = None,
    archive_dir: Path | str | None = DEFAULT_ARCHIVE_DIR,
    robots_checker: Callable[..., Any] | None = None,
    runner: Callable[[Crawl4AIConfig], Any] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
    max_host_workers: int = DEFAULT_MAX_HOST_WORKERS,
) -> dict[str, Any]:
    """Fetch configured pages with bounded cross-host concurrency.

    If no in-memory configuration is supplied, ``config_path`` is read first
    and then the explicit environment configuration is considered.  A missing
    configuration intentionally produces ``not_configured`` rather than
    scanning a directory or discovering links implicitly.
    """

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    config_errors: list[dict[str, Any]] = []
    configs_supplied = configs is not None
    if not configs_supplied:
        configs, config_errors = load_crawl4ai_configs(config_path=config_path)
    assert configs is not None  # narrowed for the direct-config path below
    # ``load_crawl4ai_configs`` performs this admission while parsing config
    # files.  In-memory callers bypass that loader, so run the same checks in
    # a serial preflight before inspecting the optional browser/TLS runtime or
    # starting any host worker.  The maps are passed through to _crawl_many to
    # avoid a second policy call (and to keep ordering observable in tests).
    admission_decisions: dict[int, source_rights.Decision] = {}
    admission_errors: dict[int, dict[str, Any]] = {}
    if configs_supplied:
        admission_decisions, admission_errors = _admission_preflight(
            configs,
            test_only_runner=runner is not None,
            observed_at=reference_time,
        )
    rights_blocked_sources = _rights_blocked_sources(config_errors)
    if not configs:
        rights_only = bool(rights_blocked_sources) and len(rights_blocked_sources) == len(
            config_errors
        )
        return {
            "provider": "Crawl4AI",
            "provider_role": "execution_layer",
            "retrieved_at": reference_time.isoformat(),
            "configured_count": 0,
            "page_count": 0,
            "pages": [],
            "errors": config_errors,
            "status": (
                "rights_blocked"
                if rights_only
                else "invalid_config"
                if config_errors
                else "not_configured"
            ),
            "checked_at": reference_time.isoformat(),
            "rights_status": (
                "source_rights_not_verified" if rights_blocked_sources else None
            ),
            "rights_blocked_count": len(rights_blocked_sources),
            "rights_blocked_sources": rights_blocked_sources,
            "network_opened": False,
            "model_eligible": False,
            "runtime_evidence": False,
            "runner_mode": "test_only" if runner is not None else "production",
            "model_use": "display_only_unstructured_until_source_parser",
            "security_policy": _security_policy(),
            **_execution_layer_metadata(configs, [], config_errors),
        }
    # If every in-memory lane was rejected during admission, do not even
    # inspect Crawl4AI's browser manager.  Central source rights must be the
    # first boundary for a blocked source, including when TLS diagnostics are
    # otherwise available.  Config-file rows have already been admitted by
    # the loader and therefore use the normal runtime probe here.
    should_check_tls = runner is None and (
        not configs_supplied or bool(admission_decisions)
    )
    tls_runtime = (
        strict_tls_runtime_status()
        if should_check_tls
        else {
            "status": "not_checked",
            "reason": (
                "custom runner injected; browser launch policy was not verified"
                if runner is not None
                else "all configured lanes were blocked before TLS runtime inspection"
            ),
        }
    )
    if should_check_tls and tls_runtime["status"] != "supported":
        preflight_error_rows = [
            admission_errors[id(config)]
            for config in configs
            if id(config) in admission_errors
        ]
        # Only lanes that passed source admission receive a TLS-runtime
        # diagnostic.  Keep parser/rights blocks for rejected peers visible
        # in declaration order instead of replacing them with a generic TLS
        # error when one admitted lane makes the global probe run.
        tls_targets = [
            config
            for config in configs
            if not configs_supplied or id(config) in admission_decisions
        ]
        tls_errors = [
            _page_error(
                config,
                observed_at=reference_time,
                stage="tls_policy",
                error=tls_runtime.get("reason", "strict TLS runtime is unavailable"),
                error_code="tls_policy_unsupported",
            )
            for config in tls_targets
        ]
        errors = [*config_errors, *preflight_error_rows, *tls_errors]
        blocked_rows = _rights_blocked_sources(errors)
        return {
            "provider": "Crawl4AI",
            "provider_role": "execution_layer",
            "retrieved_at": reference_time.isoformat(),
            "configured_count": len(configs),
            "page_count": 0,
            "pages": [],
            "errors": errors,
            "status": "blocked_tls_policy",
            "rights_blocked_count": len(blocked_rows),
            "rights_blocked_sources": blocked_rows,
            "network_opened": False,
            "model_eligible": False,
            "runtime_evidence": False,
            "runner_mode": "production",
            "model_use": "display_only_unstructured_until_source_parser",
            "tls_runtime": tls_runtime,
            "security_policy": _security_policy(),
            **_execution_layer_metadata(configs, [], errors),
        }
    result = _crawl_many(
        configs,
        reference_time=reference_time,
        archive_dir=Path(archive_dir) if archive_dir is not None else None,
        robots_checker=robots_checker,
        runner=runner or _default_crawler_runner,
        sleep_fn=sleep_fn or time.sleep,
        max_host_workers=max_host_workers,
        test_only_runner=runner is not None,
        admission_decisions=admission_decisions,
        admission_errors=admission_errors,
    )
    # A second stage is built only from records emitted by the first-stage
    # source parser.  This is intentionally a separate bounded crawl: the
    # source identity, robots decision, host/path allowlist, and raw archive
    # remain attached to every detail page.  It is never an implicit DOM
    # crawler and it never changes model admission.
    follow_configs, follow_contexts, follow_metrics, follow_rejections = _follow_up_plan(
        result.get("pages", []),
        configs,
    )
    if follow_configs:
        # Follow-up configs are fresh ``replace`` instances, so their object
        # identities are not present in the index admission maps.  Complete a
        # serial, typed preflight for the whole detail plan before starting
        # any detail worker; otherwise each host worker would independently
        # re-check central rights immediately before its own robots/browser
        # call, allowing one detail request to begin while a peer is still
        # unaudited.  Errors are passed through as well so a malformed or
        # blocked detail lane remains fail-closed without touching the network.
        detail_admission_decisions, detail_admission_errors = _admission_preflight(
            follow_configs,
            test_only_runner=runner is not None,
            observed_at=reference_time,
        )
        detail_result = _crawl_many(
            follow_configs,
            reference_time=reference_time,
            archive_dir=Path(archive_dir) if archive_dir is not None else None,
            robots_checker=robots_checker,
            runner=runner or _default_crawler_runner,
            sleep_fn=sleep_fn or time.sleep,
            max_host_workers=max_host_workers,
            page_context_by_config=follow_contexts,
            test_only_runner=runner is not None,
            admission_decisions=detail_admission_decisions,
            admission_errors=detail_admission_errors,
        )
        result["pages"] = [*result.get("pages", []), *detail_result.get("pages", [])]
        result["errors"] = [*result.get("errors", []), *detail_result.get("errors", [])]
        result["page_count"] = len(result["pages"])
        result["fresh_page_count"] = sum(1 for page in result["pages"] if page.get("stale") is not True)
        result["stale_page_count"] = sum(1 for page in result["pages"] if page.get("stale") is True)
        # The index result is the authoritative source status. A detail
        # failure is visible and degrades the aggregate, but it cannot turn a
        # successful index page into a false ``ok`` detail claim.
        if detail_result.get("errors") and result.get("fresh_page_count"):
            result["status"] = "degraded"
    current_execution = result.get("execution")
    result["execution"] = {
        **(dict(current_execution) if isinstance(current_execution, Mapping) else {}),
        "index_page_count": sum(
            1 for page in result.get("pages", []) if page.get("crawl_stage", "index") == "index"
        ),
        "detail_page_count": sum(
            1 for page in result.get("pages", []) if page.get("crawl_stage") == "detail"
        ),
        **follow_metrics,
        "follow_up_rejections": follow_rejections,
    }
    result["tls_runtime"] = tls_runtime
    if config_errors:
        result["errors"] = config_errors + result["errors"]
        if result["status"] == "ok":
            result["status"] = "degraded"
    all_rights_errors = _rights_blocked_sources(result.get("errors", []))
    result["rights_blocked_count"] = len(all_rights_errors)
    result["rights_blocked_sources"] = all_rights_errors
    result["runner_mode"] = "test_only" if runner is not None else "production"
    result["runtime_evidence"] = bool(
        runner is None and any(page.get("runtime_evidence") is not False for page in result.get("pages", []))
    )
    result["network_opened"] = bool(
        runner is None and any(page.get("network_opened") is True for page in result.get("pages", []))
    )
    result["model_eligible"] = False
    # Refresh the bounded lineage after optional detail-page fan-out and after
    # configuration diagnostics have been appended.  The execution layer
    # remains shared; each page keeps its declared fact-source identity.
    result.update(_execution_layer_metadata(configs, result.get("pages", []), result.get("errors", [])))
    return result


__all__ = [
    "Crawl4AIConfig",
    "Crawl4AIConfigError",
    "DEFAULT_ARCHIVE_DIR",
    "DEFAULT_MAX_HOST_WORKERS",
    "fetch_crawl4ai",
    "load_crawl4ai_configs",
    "strict_tls_runtime_status",
]
