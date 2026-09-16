"""Read-only adapter for the public Lazq竞彩 mirror endpoint.

The calculator page itself calls this endpoint without authentication.  The
payload contains the Chinese lottery-style markets, but the site is not an
official Sports Lottery domain and no authorization/lineage proof is present
in the response.  Records are therefore evidence-only: they may be shown as
an unverified cross-check, never as official purchase odds or model features.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


LAZQ_HOST = "api.lazq.com"
LAZQ_PATH = "/MobileJin/getJinList"
LAZQ_URL = f"https://{LAZQ_HOST}{LAZQ_PATH}"
SOURCE_NAME = "Lazq public mirror"
MAX_CONTENT_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != LAZQ_HOST
        or parsed.port not in (None, 443)
        or parsed.path != LAZQ_PATH
    ):
        raise ValueError("Lazq URL is not allowlisted")


def _odds(pool: object, keys: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(pool, dict):
        return {}
    result: dict[str, float] = {}
    for key in keys:
        try:
            value = float(pool.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 1:
            result[key] = value
    return result


def _devig(odds: dict[str, float], labels: tuple[str, ...]) -> dict[str, float] | None:
    inverse = {key: 1 / odds[key] for key in labels if key in odds}
    total = sum(inverse.values())
    if len(inverse) != len(labels) or not math.isfinite(total) or total <= 0:
        return None
    return {key: round(value / total, 8) for key, value in inverse.items()}


def _epoch(value: object) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _source(*, url: str, retrieved_at: datetime, payload: bytes) -> dict:
    return {
        "name": SOURCE_NAME,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "official_verified": False,
        "purchase_eligible": False,
        "model_eligible": False,
    }


def parse_lazq_payload(
    payload: bytes,
    *,
    retrieved_at: datetime,
    url: str = LAZQ_URL,
) -> list[dict]:
    """Parse the page-owned public calculator response without upgrading it."""

    _validate_url(url)
    observed = _utc(retrieved_at, field="retrieved_at")
    if len(payload) > MAX_CONTENT_BYTES:
        raise ValueError("Lazq response exceeded 10 MiB")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Lazq response is not valid JSON") from exc
    if (
        not isinstance(data, dict)
        or data.get("status") != 0
        or not isinstance(data.get("data"), list)
    ):
        raise ValueError("Lazq response has no public match list")
    source = _source(url=url, retrieved_at=observed, payload=payload)
    result = []
    for item in data["data"]:
        if not isinstance(item, dict):
            continue
        kickoff = _epoch(item.get("matchTime"))
        if kickoff is None or not item.get("gameId"):
            continue
        had_odds = _odds(item.get("had"), ("h", "d", "a"))
        hhad_odds = _odds(item.get("hhad"), ("h", "d", "a"))
        ttg_odds = _odds(item.get("ttg"), tuple(f"s{i}" for i in range(8)))
        hafu_odds = _odds(item.get("hafu"), ("hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"))
        raw_crs = item.get("crs") if isinstance(item.get("crs"), dict) else {}
        crs_odds = _odds(raw_crs, tuple(key for key in raw_crs if str(key).startswith("s")))
        result.append(
            {
                "game_id": str(item["gameId"]),
                "jc_id": str(item.get("jcId")) if item.get("jcId") is not None else None,
                "match_num": str(item.get("matchNumStr") or ""),
                "competition": str(item.get("competitionName") or ""),
                "home_team": str(item.get("homeTeamName") or ""),
                "away_team": str(item.get("awayTeamName") or ""),
                "kickoff_at": kickoff.isoformat(),
                "had_odds": had_odds,
                "had_probability": _devig(had_odds, ("h", "d", "a")),
                "hhad_line": (item.get("hhad") or {}).get("goal")
                if isinstance(item.get("hhad"), dict)
                else None,
                "hhad_odds": hhad_odds,
                "hhad_probability": _devig(hhad_odds, ("h", "d", "a")),
                "ttg_odds": ttg_odds,
                "ttg_probability": _devig(ttg_odds, tuple(f"s{i}" for i in range(8))),
                "hafu_odds": hafu_odds,
                "hafu_probability": _devig(
                    hafu_odds, ("hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa")
                ),
                "crs_odds": crs_odds,
                "source": source,
            }
        )
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("Lazq redirected; redirects are disabled")


def fetch_lazq_matches(
    *,
    now: datetime | None = None,
    opener=None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict:
    """Fetch the same public calculator payload used by the web page."""

    return rights_blocked_fetch_envelope(
        SourceId.LAZQ_PUBLIC_MIRROR,
        provider=SOURCE_NAME,
        now=now,
        empty_fields=("matches",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    reference = _utc(now or datetime.now(timezone.utc), field="now")
    request = urllib.request.Request(
        LAZQ_URL,
        data=b'{"cha":-2}',
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Matchline/1.0 (+public-source)",
        },
    )
    fetcher = opener or urllib.request.build_opener(_NoRedirect())
    try:
        response = fetcher.open(request, timeout=DEFAULT_TIMEOUT_SECONDS)
        with response:
            final_url = getattr(response, "geturl", lambda: LAZQ_URL)()
            _validate_url(final_url)
            payload = response.read(MAX_CONTENT_BYTES + 1)
        if len(payload) > MAX_CONTENT_BYTES:
            raise ValueError("Lazq response exceeded 10 MiB")
        matches = parse_lazq_payload(payload, retrieved_at=reference, url=final_url)
        return {
            "provider": SOURCE_NAME,
            "retrieved_at": reference.isoformat(),
            "matches": matches,
            "errors": [],
            "status": "evidence_only_unverified_mirror",
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "provider": SOURCE_NAME,
            "retrieved_at": reference.isoformat(),
            "matches": [],
            "errors": [{"stage": "fetch_or_parse", "error": str(exc)}],
            "status": "unavailable",
        }


__all__ = ["LAZQ_URL", "fetch_lazq_matches", "parse_lazq_payload"]
