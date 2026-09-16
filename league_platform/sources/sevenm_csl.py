"""Parse 7M's public Chinese Super League season fixture script.

7M publishes a small JavaScript data file for each historical season.  The
file is a public result index, not an odds archive: this adapter deliberately
parses only exact kickoff, teams, full-time score and half-time score.  It is
opt-in and marks the provider licence as requiring review, so callers must
archive the response and complete a source review before using the rows for a
model or a backtest.
"""

from __future__ import annotations

import ast
import hashlib
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from league_platform.domain import Match, Score
from league_platform.identity import team_id
from league_platform.source_rights import SourceId, rights_blocked_fetch_envelope


HOST = "data.7msport.com"
COMPETITION_ID = "csl"
LEAGUE_ID = "152"
TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 20 * 1024 * 1024
URL_TEMPLATE = "https://data.7msport.com/history_matches_data/{season}/152/en/fixture.js"
_SEASON_RE = re.compile(r"^(?:19|20)\d{2}(?:-(?:19|20)\d{2})?$")
_SCORE_RE = re.compile(r"^(\d+)-(\d+)(?:\((\d+)-(\d+)\))?$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise urllib.error.URLError("7M redirected; redirects are disabled")


def season_url(season: str) -> str:
    """Build the only URL accepted by the adapter."""

    if not isinstance(season, str) or not _SEASON_RE.fullmatch(season):
        raise ValueError("7M CSL season must be YYYY or YYYY-YYYY")
    return URL_TEMPLATE.format(season=season)


def _validate_url(url: str, *, season: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != f"/history_matches_data/{season}/{LEAGUE_ID}/en/fixture.js"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("7M CSL URL is not allowlisted")


def _extract_array(payload: bytes, name: str) -> list[object]:
    """Extract one JavaScript array without executing JavaScript."""

    text = payload.decode("utf-8-sig")
    marker = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*\[", text)
    if marker is None:
        raise ValueError(f"7M fixture script is missing {name}")
    start = text.find("[", marker.start())
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                source = text[start : index + 1]
                try:
                    value = ast.literal_eval(source)
                except (SyntaxError, ValueError) as exc:
                    raise ValueError(f"7M {name} array is not a literal") from exc
                if not isinstance(value, list):
                    raise ValueError(f"7M {name} must be an array")
                return value
    raise ValueError(f"7M {name} array is unterminated")


def _parse_score(value: object) -> Score | None:
    if not isinstance(value, str):
        return None
    match = _SCORE_RE.fullmatch(value.strip())
    if match is None:
        return None
    home, away, halftime_home, halftime_away = match.groups()
    return Score(
        home=int(home),
        away=int(away),
        halftime_home=int(halftime_home) if halftime_home is not None else None,
        halftime_away=int(halftime_away) if halftime_away is not None else None,
    )


def _parse_kickoff(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        kickoff = datetime.strptime(value.strip(), "%Y,%m,%d,%H,%M,%S")
    except ValueError:
        return None
    # The provider sometimes serves a postponed match in a later season's
    # script. Keep the exact provider value rather than guessing a date.
    return kickoff.replace(tzinfo=TIMEZONE)


def parse_sevenm_csl_fixture_script(
    payload: bytes,
    *,
    season: str,
    url: str | None = None,
    source_file: str | None = None,
) -> list[Match]:
    """Parse a 7M season script into finished CSL matches.

    Rows with an unfinished ``VS`` score, missing names, or malformed times
    are retained by the source audit but omitted from the normalized result.
    """

    url = url or season_url(season)
    _validate_url(url, season=season)
    if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
        raise ValueError("7M CSL fixture script exceeded 20 MiB or was not bytes")
    arrays = {
        name: _extract_array(payload, name)
        for name in ("Tmp_bh_Arr", "Time_Arr", "Scores_Arr", "TeamA_Arr", "TeamB_Arr")
    }
    lengths = {len(value) for value in arrays.values()}
    if len(lengths) != 1:
        raise ValueError("7M CSL fixture arrays have inconsistent lengths")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    matches: list[Match] = []
    for provider_id, kickoff_value, score_value, home, away in zip(
        arrays["Tmp_bh_Arr"],
        arrays["Time_Arr"],
        arrays["Scores_Arr"],
        arrays["TeamA_Arr"],
        arrays["TeamB_Arr"],
        strict=True,
    ):
        if not isinstance(provider_id, (int, str)) or isinstance(provider_id, bool):
            continue
        if not isinstance(home, str) or not isinstance(away, str):
            continue
        home = home.strip()
        away = away.strip()
        if not home or not away:
            continue
        kickoff = _parse_kickoff(kickoff_value)
        score = _parse_score(score_value)
        if kickoff is None or score is None:
            continue
        home_id = team_id(COMPETITION_ID, home)
        away_id = team_id(COMPETITION_ID, away)
        identity = f"{COMPETITION_ID}|{season}|{kickoff.isoformat()}|{home_id}|{away_id}"
        match_id = hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        matches.append(
            Match(
                id=match_id,
                competition_id=COMPETITION_ID,
                season=season,
                kickoff_at=kickoff,
                home_team=home,
                away_team=away,
                home_team_id=home_id,
                away_team_id=away_id,
                status="finished",
                score=score,
                market_probability=None,
                source_file=source_file or url,
                source_sha256=source_sha256,
                provider_fixture_id=f"7m:{provider_id}",
                source_name="7M Sports",
                source_license_status="provider_terms_require_review",
                kickoff_time_quality="exact",
                kickoff_time_source="7M Sports",
            )
        )
    ids = [match.id for match in matches]
    if len(ids) != len(set(ids)):
        raise ValueError("7M CSL fixture script contains duplicate normalized fixtures")
    return matches


def fetch_sevenm_csl_season(
    season: str,
    *,
    now: datetime | None = None,
    opener: Callable[..., object] | object | None = None,
    authorization_reference: object | None = None,
    operator_registry: object | None = None,
    config: object | None = None,
) -> dict[str, object]:
    """Fetch one bounded, public 7M season script without following redirects."""

    return rights_blocked_fetch_envelope(
        SourceId.SEVENM_CSL_PUBLIC_FIXTURE_SCRIPT,
        provider="7M Sports",
        now=now,
        empty_fields=("matches",),
        authorization_reference=authorization_reference,
        operator_registry=operator_registry,
        config=config,
    )

    url = season_url(season)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/javascript, */*", "User-Agent": "Matchline/1.0"},
    )
    fetcher = opener or urllib.request.build_opener(_NoRedirect())
    open_method = getattr(fetcher, "open", None) or fetcher
    with open_method(request, timeout=30) as response:  # type: ignore[operator]
        final_url = str(response.geturl())
        _validate_url(final_url, season=season)
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("7M CSL fixture script exceeded 20 MiB")
    matches = parse_sevenm_csl_fixture_script(payload, season=season, url=final_url)
    return {
        "provider": "7M Sports",
        "competition_id": COMPETITION_ID,
        "season": season,
        "url": final_url,
        "retrieved_at": reference.astimezone(timezone.utc).isoformat(),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "matches": matches,
        "status": "ok" if matches else "degraded",
        "model_eligible": False,
        "model_exclusion_reason": "provider_terms_require_review_and_no_market_fields",
    }


__all__ = [
    "COMPETITION_ID",
    "HOST",
    "URL_TEMPLATE",
    "fetch_sevenm_csl_season",
    "parse_sevenm_csl_fixture_script",
    "season_url",
]
