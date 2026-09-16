"""Resolve public news leads to canonical fixtures or teams at publication time.

RSS is intentionally collected as a lead stream.  This module is the narrow
boundary between that unstructured stream and the typed Sites ``news_items``
projection: it binds a fixture only when both sides of exactly one fixture are
explicitly present, or binds a team only when one exact team variant maps to a
single provider identity in a nearby fixture window.  In both cases
publication/observation timestamps are pre-kickoff.  Ambiguous, late,
incomplete, or provider-unidentified rows remain unbound so the raw
intelligence ledger is still auditable without creating a false join.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalised(value: object) -> tuple[str, tuple[str, ...]]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = tuple(match.group(0) for match in _WORD_RE.finditer(text))
    compact = "".join(tokens)
    return compact, tokens


def _variants(fixture: Mapping[str, object], side: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    values: list[object] = [fixture.get(f"{side}_team"), fixture.get(f"{side}_team_en")]
    aliases = fixture.get(f"{side}_team_aliases")
    if isinstance(aliases, (list, tuple)):
        values.extend(aliases)
    result: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for value in values:
        compact, tokens = _normalised(value)
        if not compact or len(compact) < 2:
            continue
        key = (compact, tokens)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return tuple(result)


def _contains_team(text: tuple[str, tuple[str, ...]], variant: tuple[str, tuple[str, ...]]) -> bool:
    compact_text, text_tokens = text
    compact_variant, variant_tokens = variant
    if not compact_variant:
        return False
    # CJK names have no whitespace token boundary.  Compact containment is
    # appropriate there; Latin names use exact token sequences to avoid a
    # short team such as "Roma" matching a longer unrelated word.
    if any(ord(char) > 127 for char in compact_variant):
        return compact_variant in compact_text
    if len(variant_tokens) > 1:
        width = len(variant_tokens)
        return any(text_tokens[index:index + width] == variant_tokens for index in range(len(text_tokens) - width + 1))
    return variant_tokens[0] in text_tokens


def _fixture_candidates(fixtures: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for fixture in fixtures:
        if not isinstance(fixture, Mapping):
            continue
        fixture_id = fixture.get("id")
        kickoff = _utc(fixture.get("kickoff_at"))
        home = _variants(fixture, "home")
        away = _variants(fixture, "away")
        if not isinstance(fixture_id, str) or not fixture_id or kickoff is None or not home or not away:
            continue
        candidates.append({
            "id": fixture_id,
            "kickoff": kickoff,
            "home_name": str(fixture.get("home_team") or fixture.get("home_team_en") or "").strip(),
            "away_name": str(fixture.get("away_team") or fixture.get("away_team_en") or "").strip(),
            "home": home,
            "away": away,
            "home_provider_team_id": _provider_team_id(fixture.get("home_provider_team_id")),
            "away_provider_team_id": _provider_team_id(fixture.get("away_provider_team_id")),
        })
    return candidates


def _provider_team_id(value: object) -> str | None:
    """Return a bounded provider id, never a name-derived identity."""

    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        if text and len(text) <= 160 and re.fullmatch(r"[A-Za-z0-9:_-]+", text):
            return text
    return None


@dataclass(frozen=True)
class _FixtureIndex:
    """Name index used to avoid scanning every fixture for every news row."""

    candidates: tuple[dict[str, object], ...]
    home_latin: dict[tuple[str, ...], frozenset[int]]
    away_latin: dict[tuple[str, ...], frozenset[int]]
    home_cjk: tuple[tuple[str, int], ...]
    away_cjk: tuple[tuple[str, int], ...]
    home_max_width: int
    away_max_width: int

    @classmethod
    def build(cls, candidates: list[dict[str, object]]) -> "_FixtureIndex":
        home_latin: dict[tuple[str, ...], set[int]] = {}
        away_latin: dict[tuple[str, ...], set[int]] = {}
        home_cjk: list[tuple[str, int]] = []
        away_cjk: list[tuple[str, int]] = []
        home_max_width = 1
        away_max_width = 1
        for index, candidate in enumerate(candidates):
            for side, latin, cjk in (
                ("home", home_latin, home_cjk),
                ("away", away_latin, away_cjk),
            ):
                variants = candidate[side]
                if not isinstance(variants, tuple):
                    continue
                for compact, tokens in variants:
                    if any(ord(char) > 127 for char in compact):
                        cjk.append((compact, index))
                        continue
                    latin.setdefault(tokens, set()).add(index)
                    if side == "home":
                        home_max_width = max(home_max_width, len(tokens))
                    else:
                        away_max_width = max(away_max_width, len(tokens))
        return cls(
            candidates=tuple(candidates),
            home_latin={key: frozenset(value) for key, value in home_latin.items()},
            away_latin={key: frozenset(value) for key, value in away_latin.items()},
            home_cjk=tuple(home_cjk),
            away_cjk=tuple(away_cjk),
            home_max_width=home_max_width,
            away_max_width=away_max_width,
        )

    def _side_hits(self, text: tuple[str, tuple[str, ...]], side: str) -> set[int]:
        compact_text, text_tokens = text
        latin = self.home_latin if side == "home" else self.away_latin
        cjk = self.home_cjk if side == "home" else self.away_cjk
        max_width = self.home_max_width if side == "home" else self.away_max_width
        hits: set[int] = set()
        for token in text_tokens:
            hits.update(latin.get((token,), ()))
        max_width = min(max_width, len(text_tokens))
        for width in range(2, max_width + 1):
            for start in range(len(text_tokens) - width + 1):
                hits.update(latin.get(text_tokens[start:start + width], ()))
        if compact_text:
            hits.update(index for compact, index in cjk if compact in compact_text)
        return hits

    def pair_hits(self, text: tuple[str, tuple[str, ...]]) -> set[int]:
        return self._side_hits(text, "home") & self._side_hits(text, "away")


def _row_text(row: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    payload = row.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    parts = [payload.get("title"), payload.get("summary")]
    return _normalised(" ".join(str(value) for value in parts if isinstance(value, str)))


def _match_row(
    row: Mapping[str, object],
    fixture_index: _FixtureIndex,
    *,
    max_lead_days: int,
) -> tuple[str | None, str]:
    payload = row.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    effective = _utc(row.get("effective_at") or payload.get("published_at") or payload.get("publishedAt"))
    observed = _utc(row.get("observed_at"))
    if effective is None or observed is None:
        return None, "missing_causal_timestamp"
    text = _row_text(row)
    candidates: list[dict[str, object]] = []
    had_early_pair = False
    for candidate_index in sorted(fixture_index.pair_hits(text)):
        fixture = fixture_index.candidates[candidate_index]
        kickoff = fixture["kickoff"]
        if not isinstance(kickoff, datetime):
            continue
        if effective > kickoff or observed > kickoff:
            continue
        if kickoff - effective > timedelta(days=max_lead_days):
            had_early_pair = True
            continue
        candidates.append(fixture)
    if len(candidates) == 1:
        return str(candidates[0]["id"]), "exact_team_pair_pre_kickoff"
    if len(candidates) > 1:
        return None, "ambiguous_team_pair_fixture"
    if had_early_pair:
        return None, "news_too_early_for_fixture"
    # Distinguish a clear timestamp failure from an item which simply did not
    # contain enough fixture-specific information.  Both remain display-only.
    return None, "no_exact_team_pair_pre_kickoff"


def _match_team_row(
    row: Mapping[str, object],
    fixture_index: _FixtureIndex,
    *,
    max_lead_days: int,
) -> tuple[dict[str, object] | None, str]:
    """Resolve one article to one canonical provider team for display only.

    A team-level join is intentionally narrower than a keyword search.  The
    team name must be present as an exact normalized variant, both causal
    timestamps must precede a nearby fixture, and the fixture source must
    expose a provider team id.  Multiple matching teams or missing provider
    identities stay unbound.
    """

    payload = row.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    effective = _utc(row.get("effective_at") or payload.get("published_at") or payload.get("publishedAt"))
    observed = _utc(row.get("observed_at"))
    if effective is None or observed is None:
        return None, "missing_causal_timestamp"
    text = _row_text(row)
    matches: dict[str, dict[str, object]] = {}
    had_early = False
    matched_without_provider = False
    for candidate in fixture_index.candidates:
        kickoff = candidate.get("kickoff")
        if not isinstance(kickoff, datetime):
            continue
        if effective > kickoff or observed > kickoff:
            continue
        if kickoff - effective > timedelta(days=max_lead_days):
            had_early = True
            continue
        for side in ("home", "away"):
            variants = candidate.get(side)
            if not isinstance(variants, tuple) or not any(_contains_team(text, variant) for variant in variants):
                continue
            provider_team_id = candidate.get(f"{side}_provider_team_id")
            display_name = str(candidate.get(f"{side}_name") or "").strip()
            if not display_name:
                display_name = next((value[0] for value in variants if _contains_team(text, value)), variants[0][0])
            if not isinstance(provider_team_id, str) or not provider_team_id:
                matched_without_provider = True
                continue
            # Provider identity is the canonical key.  A different provider
            # id for the same text is treated as a real ambiguity rather than
            # silently merged by name.
            matches.setdefault(provider_team_id, {
                "provider_team_id": provider_team_id,
                "team_name": display_name,
                "provider": "ESPN",
            })
    if len(matches) == 1:
        return next(iter(matches.values())), "exact_team_pre_kickoff_recent"
    if len(matches) > 1:
        return None, "ambiguous_team_entity"
    if had_early:
        return None, "news_too_early_for_team"
    if matched_without_provider:
        return None, "team_provider_id_unavailable"
    return None, "no_exact_team_pre_kickoff"


def resolve_news_rows(
    rows: list[dict],
    fixtures: Iterable[Mapping[str, object]],
    *,
    max_lead_days: int = 16,
) -> tuple[list[dict], dict[str, object]]:
    """Return wire-ready rows and bounded entity-resolution diagnostics.

    The input rows are copied; the append-only local ledger is never rewritten.
    Only ``news_fact_candidate`` rows with an unbound ``news`` entity are
    eligible.  ``max_lead_days`` matches the current 16-day live schedule
    horizon, preventing an old article about the same team or pair from being
    attached to a later fixture.  A resolved row keeps its original evidence
    and adds a compact resolution envelope to the payload for audit/UI display.
    It remains ``enters_model=false`` regardless of the fixture/team join.
    """

    if isinstance(max_lead_days, bool) or not isinstance(max_lead_days, int) or max_lead_days < 0:
        raise ValueError("max_lead_days must be a non-negative integer")
    candidates = _fixture_candidates(fixtures)
    fixture_index = _FixtureIndex.build(candidates)
    output: list[dict] = []
    reasons: Counter[str] = Counter()
    input_count = 0
    linked_count = 0
    fixture_linked_count = 0
    team_linked_count = 0
    for original in rows:
        row = dict(original)
        if row.get("kind") != "news_fact_candidate" or row.get("entity_type") != "news":
            output.append(row)
            continue
        input_count += 1
        fixture_id, reason = _match_row(row, fixture_index, max_lead_days=max_lead_days)
        if fixture_id is None:
            team_resolution, team_reason = _match_team_row(row, fixture_index, max_lead_days=max_lead_days)
            if team_resolution is None:
                if (
                    reason == "no_exact_team_pair_pre_kickoff"
                    and team_reason == "no_exact_team_pre_kickoff"
                    and fixture_index.pair_hits(_row_text(row))
                ):
                    # When both names are present but the article is late,
                    # keep the more specific pair-level diagnostic.
                    reasons[reason] += 1
                else:
                    reasons[team_reason if reason == "no_exact_team_pair_pre_kickoff" else reason] += 1
                output.append(row)
                continue
            payload = row.get("payload")
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            payload["team_resolution"] = {
                "status": "resolved",
                "method": "exact_team_pre_kickoff_recent_v1",
                "team_provider_id": team_resolution["provider_team_id"],
                "canonical_team": team_resolution["team_name"],
                "provider": team_resolution["provider"],
                "confidence": 0.94,
                "model_use": "display_only",
            }
            row["payload"] = payload
            row["enters_model"] = False
            row["model_exclusion_reason"] = "news_fact_not_structured_model_feature"
            linked_count += 1
            team_linked_count += 1
            output.append(row)
            continue
        payload = row.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        payload["match_resolution"] = {
            "status": "resolved",
            "method": "exact_team_pair_pre_kickoff_recent_v2",
            "fixture_id": fixture_id,
            "confidence": 0.98,
            "model_use": "display_only",
        }
        row["payload"] = payload
        row["entity_type"] = "fixture"
        row["entity_id"] = fixture_id
        # A pair join is useful for the typed news table, but the source is
        # still an unstructured RSS lead and cannot become a model feature.
        row["enters_model"] = False
        row["model_exclusion_reason"] = "news_fact_not_structured_model_feature"
        linked_count += 1
        fixture_linked_count += 1
        output.append(row)
    return output, {
        "algorithm": "exact_team_pair_or_team_pre_kickoff_recent_v3",
        "model_use": "display_only",
        "candidate_fixture_count": len(candidates),
        "input_news_rows": input_count,
        "linked_news_rows": linked_count,
        "fixture_linked_news_rows": fixture_linked_count,
        "team_linked_news_rows": team_linked_count,
        "unlinked_news_rows": input_count - linked_count,
        "unlinked_reasons": dict(sorted(reasons.items())),
    }


__all__ = ["resolve_news_rows"]
