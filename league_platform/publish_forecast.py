"""Publish immutable prospective forecasts to the protected Matchline Sites API.

The local archive is the causal source of truth.  This command only publishes
non-conflict rows and requires either exact native identities or an explicitly
namespaced OpenFootball synthetic alias from the current source snapshot before
it can register a D1 fixture.  It never sends outcomes, and it keeps all writes
in the ``research_only`` state.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from league_platform.fixture_feed import fixture_rows
from league_platform.fixture_serving import require_fixture_serving
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_ALLOWED_URLS
from league_platform.publish_intelligence import (
    PublicationReceiptStore,
    PublicationMaturityContext,
    ProducerAttestationContext,
    build_producer_attestation_context,
    build_publication_maturity_context,
    publication_receipt_identity,
    require_publication_maturity,
    resolve_publication_epoch,
    publish_payload as _publish_resilient_payload,
)
from league_platform.publish_snapshot import (
    fixture_publication_identity,
    require_openfootball_raw_producer_receipt,
)
from league_platform.source_rights import SourceId, UseCase, decide_source_rights


MAX_PAYLOAD_BYTES = 512 * 1024
SUPPORTED_STAGES = ("t_minus_24h", "t_minus_6h", "t_minus_90m", "lineup_confirmation")
EXPECTED_FEATURES = ("recent_xg", "market", "weather", "injuries", "lineups")
DISTRIBUTION_TOLERANCE = 0.01
DERIVED_DISTRIBUTION_TOLERANCE = 0.02
_FEATURE_SOURCE_IDS = {
    "OpenFootball": SourceId.OPENFOOTBALL_CURRENT,
    "ESPN": SourceId.ESPN_SCHEDULE_SUMMARY,
    "ESPN event summary": SourceId.ESPN_MARKET_SUMMARY,
    "Understat": SourceId.UNDERSTAT_XG,
    "Sports Lottery": SourceId.SPORTS_LOTTERY_OFFICIAL,
    "Open-Meteo": SourceId.OPEN_METEO_WEATHER,
    "SofaScore": SourceId.SOFASCORE_PREMATCH,
    "Premier League official": SourceId.OFFICIAL_PREMIER_LEAGUE_LINEUPS,
    "LaLiga official": SourceId.OFFICIAL_LALIGA_LINEUPS,
    "Bundesliga official": SourceId.OFFICIAL_BUNDESLIGA_LINEUPS,
    "Serie A official": SourceId.OFFICIAL_SERIE_A_LINEUPS,
    "Ligue 1 official": SourceId.OFFICIAL_LIGUE1_LINEUPS,
}


def _require_feature_source(
    source: Mapping[str, Any],
) -> str:
    provider = source.get("name") or source.get("source_name")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("forecast_feature_source_missing")
    source_id = _FEATURE_SOURCE_IDS.get(provider)
    if source_id is None:
        raise ValueError(f"source_rights_unknown_provider:{provider}")
    source_url = source.get("url") or source.get("source_url")
    if (
        source_id is SourceId.OPENFOOTBALL_CURRENT
        and source_url not in OPENFOOTBALL_ALLOWED_URLS
    ):
        raise ValueError("openfootball_source_contract_unverified")
    for use_case in (UseCase.MODEL_INPUT, UseCase.REDISTRIBUTION):
        decision = decide_source_rights(source_id, use_case)
        if not decision.is_allowed:
            raise ValueError(
                f"source_rights_blocked:{source_id.value}:{use_case.value}"
            )
    return source_id.value


def _require_prediction_feature_rights(
    prediction: Mapping[str, Any],
) -> list[dict[str, str]]:
    material_sources: dict[tuple[str, str], dict[str, str]] = {}

    def validate_material(source: Mapping[str, Any]) -> None:
        _require_feature_source(source)
        name = source.get("name") or source.get("source_name")
        url = source.get("url") or source.get("source_url")
        if not isinstance(name, str) or not isinstance(url, str) or not url:
            raise ValueError("forecast_feature_source_url_missing")
        material_sources[(name, url)] = {"name": name, "url": url}

    raw_sources = prediction.get("live_feature_sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("forecast_feature_sources_malformed")
    source_fields: set[str] = set()
    for source in raw_sources:
        if not isinstance(source, Mapping):
            raise ValueError("forecast_feature_sources_malformed")
        field = source.get("field")
        if not isinstance(field, str) or not field.strip():
            raise ValueError("forecast_feature_source_field_missing")
        validate_material(source)
        source_fields.add(field)

    raw_feature_fields = prediction.get("live_feature_fields", [])
    if not isinstance(raw_feature_fields, list) or any(
        not isinstance(field, str) or not field.strip()
        for field in raw_feature_fields
    ):
        raise ValueError("forecast_feature_fields_malformed")
    for field in raw_feature_fields:
        matching = (
            field in source_fields
            or field == "recent_xg"
            and {"recent_xg_home", "recent_xg_away"}.issubset(source_fields)
        )
        if not matching:
            raise ValueError(f"forecast_feature_source_missing:{field}")

    if prediction.get("market_probability") is not None and "market_1x2" not in source_fields:
        raise ValueError("forecast_feature_source_missing:market_probability")

    factor_trace = prediction.get("factor_trace")
    if factor_trace is None:
        return [material_sources[key] for key in sorted(material_sources)]
    if not isinstance(factor_trace, Mapping):
        raise ValueError("forecast_factor_trace_malformed")
    for collection_name in ("steps", "not_applied"):
        rows = factor_trace.get(collection_name, [])
        if not isinstance(rows, list):
            raise ValueError("forecast_factor_trace_malformed")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("forecast_factor_trace_malformed")
            source_name = row.get("source_name")
            source_url = row.get("source_url")
            if source_name in (None, "") and source_url in (None, ""):
                continue
            if (
                row.get("id") == "historical_goal_rates"
                and source_name == "strict causal historical state"
                and source_url in (None, "")
            ):
                continue
            validate_material(
                {"source_name": source_name, "source_url": source_url}
            )
    return [material_sources[key] for key in sorted(material_sources)]
COMPETITIONS: dict[str, tuple[str, str]] = {
    "csl": ("中超", "中国"),
    "premier-league": ("英超", "England"),
    "championship": ("英冠", "England"),
    "la-liga": ("西甲", "Spain"),
    "bundesliga": ("德甲", "Germany"),
    "serie-a": ("意甲", "Italy"),
    "ligue-1": ("法甲", "France"),
    "five-hundred-league": ("500联赛", "China"),
}


@dataclass(frozen=True)
class ArchiveRow:
    record: Mapping[str, Any]
    prediction: Mapping[str, Any]


def _iso(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _distribution(value: Any, name: str, *, allowed: Callable[[str], bool] | None = None) -> dict[str, float]:
    values = _json_object(value, name)
    if not values or len(values) > 256:
        raise ValueError(f"{name} must contain between 1 and 256 entries")
    result: dict[str, float] = {}
    for label, raw in values.items():
        label = str(label)
        if not label or len(label) > 80 or (allowed is not None and not allowed(label)):
            raise ValueError(f"{name} contains an invalid label")
        try:
            parsed = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}.{label} must be numeric") from exc
        if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
            raise ValueError(f"{name}.{label} must be between 0 and 1")
        result[label] = parsed
    if abs(sum(result.values()) - 1.0) > DISTRIBUTION_TOLERANCE:
        raise ValueError(f"{name} probabilities must sum to one")
    return result


_SCORELINE_RE = re.compile(r"^(?:0|[1-9]\d{0,1})[-:](?:0|[1-9]\d{0,1})$")


def _scoreline_matrix(value: Any, name: str = "scoreline_matrix") -> dict[str, float]:
    source = _json_object(value, name)
    flattened: dict[str, float] = {}
    flat_entries = [(str(key), item) for key, item in source.items() if _SCORELINE_RE.fullmatch(str(key))]
    if flat_entries and len(flat_entries) != len(source):
        raise ValueError(f"{name} cannot mix flat and nested scoreline forms")
    if flat_entries:
        for raw_label, raw_probability in flat_entries:
            home, away = re.split(r"[-:]", raw_label, maxsplit=1)
            label = f"{int(home)}-{int(away)}"
            if label in flattened:
                raise ValueError(f"{name} contains duplicate scorelines")
            try:
                parsed = float(raw_probability)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}.{raw_label} must be numeric") from exc
            if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
                raise ValueError(f"{name}.{raw_label} must be between 0 and 1")
            flattened[label] = parsed
    else:
        for raw_home, raw_away_values in source.items():
            home = str(raw_home)
            if not re.fullmatch(r"(?:0|[1-9]\d{0,1})", home) or int(home) > 20:
                raise ValueError(f"{name} contains an invalid nested scoreline row")
            away_values = _json_object(raw_away_values, f"{name}.{home}")
            for raw_away, raw_probability in away_values.items():
                away = str(raw_away)
                if not re.fullmatch(r"(?:0|[1-9]\d{0,1})", away) or int(away) > 20:
                    raise ValueError(f"{name} contains an invalid nested scoreline")
                label = f"{int(home)}-{int(away)}"
                if label in flattened:
                    raise ValueError(f"{name} contains duplicate scorelines")
                try:
                    parsed = float(raw_probability)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{name}.{home}.{away} must be numeric") from exc
                if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
                    raise ValueError(f"{name}.{home}.{away} must be between 0 and 1")
                flattened[label] = parsed
    if not flattened or len(flattened) > 256:
        raise ValueError(f"{name} must contain between 1 and 256 scorelines")
    if abs(sum(flattened.values()) - 1.0) > DISTRIBUTION_TOLERANCE:
        raise ValueError(f"{name} probabilities must sum to one")
    return flattened


def _one_x_two_from_matrix(matrix: Mapping[str, float]) -> dict[str, float]:
    result = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for label, value in matrix.items():
        home, away = (int(item) for item in label.split("-", 1))
        result["home" if home > away else "draw" if home == away else "away"] += value
    return result


def _total_goals_from_matrix(matrix: Mapping[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for label, value in matrix.items():
        home, away = (int(item) for item in label.split("-", 1))
        key = str(home + away) if home + away < 5 else "5+"
        result[key] = result.get(key, 0.0) + value
    return result


def _assert_same_distribution(declared: Mapping[str, float], derived: Mapping[str, float], name: str) -> None:
    for label in set(declared) | set(derived):
        if abs(float(declared.get(label, 0.0)) - float(derived.get(label, 0.0))) > DERIVED_DISTRIBUTION_TOLERANCE:
            raise ValueError(f"{name} does not match scoreline_matrix")


def _validate_stage_distributions(prediction: Mapping[str, Any], matrix: Mapping[str, float]) -> None:
    stage_probability = _probability(prediction)
    _assert_same_distribution(stage_probability, _one_x_two_from_matrix(matrix), "1X2 probability")
    total_goals = prediction.get("total_goals_probability")
    if total_goals is not None:
        declared = _distribution(total_goals, "total_goals_probability", allowed=lambda label: bool(re.fullmatch(r"(?:0|[1-9]\d{0,1}|5\+)", label)))
        _assert_same_distribution(declared, _total_goals_from_matrix(matrix), "total_goals_probability")
    half_full = prediction.get("half_full_probability")
    if half_full is not None:
        _distribution(half_full, "half_full_probability", allowed=lambda label: bool(re.fullmatch(r"[HDA]/[HDA]", label)))
    handicap = prediction.get("handicap_probability")
    if handicap is not None:
        _distribution(handicap, "handicap_probability", allowed=lambda label: label in {"win", "push", "loss", "half_win", "half_loss"})
    total_over_under = prediction.get("total_over_under_probability")
    if total_over_under is not None:
        _distribution(total_over_under, "total_over_under_probability", allowed=lambda label: label in {"over", "push", "under", "half_win", "half_loss"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_MODEL_LOCK_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _normalized_model_lock_sha(value: Any, *, field: str = "model_version_sha256") -> str | None:
    """Return a canonical lock digest, failing closed for malformed values."""

    if value is None:
        return None
    if not isinstance(value, str) or not _MODEL_LOCK_SHA_RE.fullmatch(value):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


def publication_model_version(model_version: Any, model_version_sha256: Any = None) -> str:
    """Build the immutable D1 model identity for one locked model file.

    The human-readable model name remains unchanged when no digest is
    available for backwards compatibility.  Once a lock digest is present,
    the first 16 hexadecimal characters become part of the D1 identity while
    the complete digest is retained in the feature/provenance payload.
    """

    base = str(model_version or "").strip()
    if not base:
        raise ValueError("model_version is required")
    digest = _normalized_model_lock_sha(model_version_sha256)
    return f"{base}@lock-{digest[:16]}" if digest else base


def publication_schema_version(model_version_sha256: Any = None) -> str:
    """Return a lock-specific feature schema identity when available."""

    digest = _normalized_model_lock_sha(model_version_sha256)
    return f"prospective-forecast-v1@lock-{digest[:16]}" if digest else "prospective-forecast-v1"


def _fixture_index(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["id"]): item
        for item in fixture_rows(snapshot)
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _available_features(prediction: Mapping[str, Any]) -> set[str]:
    fields = prediction.get("live_feature_fields")
    values = {str(item) for item in fields} if isinstance(fields, list) else set()
    available: set[str] = set()
    if "recent_xg" in values:
        available.add("recent_xg")
    if values.intersection({"market_1x2", "sports_lottery_hhad", "odds", "market"}):
        available.add("market")
    if "weather" in values:
        available.add("weather")
    if values.intersection({"injuries", "player_status"}):
        available.add("injuries")
    if values.intersection({"lineups", "official_lineup"}):
        available.add("lineups")
    if prediction.get("freeze_stage") == "lineup_confirmation" and prediction.get("lineup_confirmation_fingerprint"):
        available.add("lineups")
    return available


def _stage_coverage(prediction: Mapping[str, Any]) -> tuple[float, list[str]]:
    available = _available_features(prediction)
    missing = [name for name in EXPECTED_FEATURES if name not in available]
    return round(len(available) / len(EXPECTED_FEATURES), 6), missing


def _probability(prediction: Mapping[str, Any]) -> dict[str, float]:
    raw = prediction.get("scoreline_probability") or prediction.get("primary_probability_1x2") or prediction.get("probability_1x2_from_scoreline")
    values = _json_object(raw, "1X2 probability")
    try:
        normalized = {"home": float(values["home"]), "draw": float(values["draw"]), "away": float(values["away"])}
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("1X2 probability is not a valid distribution") from exc
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in normalized.values()) or abs(sum(normalized.values()) - 1) > 0.01:
        raise ValueError("1X2 probability is not a valid distribution")
    return normalized


def _market_probability(prediction: Mapping[str, Any]) -> dict[str, float] | None:
    raw = prediction.get("market_probability")
    if raw is None:
        return None
    values = _json_object(raw, "market probability")
    try:
        result = {"home": float(values["home"]), "draw": float(values["draw"]), "away": float(values["away"])}
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("market probability is not a valid distribution") from exc
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in result.values()) or abs(sum(result.values()) - 1) > 0.01:
        raise ValueError("market probability is not a valid distribution")
    return result


def _expected_goals(value: Any) -> dict[str, float | None]:
    values = _json_object(value, "expected_goals")
    result: dict[str, float | None] = {}
    for label in ("home", "away"):
        if label not in values:
            raise ValueError(f"expected_goals must contain {label}")
        raw = values.get(label)
        if raw is None:
            result[label] = None
            continue
        try:
            parsed = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected_goals.{label} must be numeric") from exc
        if not math.isfinite(parsed) or parsed < 0 or parsed > 20:
            raise ValueError(f"expected_goals.{label} must be between 0 and 20")
        result[label] = parsed
    return result


def _safe_feature_payload(
    prediction: Mapping[str, Any],
    *,
    model_version_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage, missing = _stage_coverage(prediction)
    digest = _normalized_model_lock_sha(model_version_sha256)
    base_model_version = prediction.get("model_version")
    features = {
        "modelVersion": (
            publication_model_version(base_model_version, digest)
            if base_model_version
            else base_model_version
        ),
        "modelLockSha256": digest,
        "featureTimes": list(prediction.get("feature_times", [])) if isinstance(prediction.get("feature_times"), list) else [],
        "liveFeatureFields": list(prediction.get("live_feature_fields", [])) if isinstance(prediction.get("live_feature_fields"), list) else [],
        "featureCoverage": coverage,
        "missingFeatures": missing,
        "expectedGoals": prediction.get("expected_goals"),
        "marketProbability": prediction.get("market_probability"),
        # The trace is a bounded audit projection of declared model steps. It
        # is not used to calculate the forecast and remains safe to omit for
        # older archive rows.
        "modelFactorTrace": prediction.get("factor_trace"),
        "handicapLine": prediction.get("handicap_line"),
        "lineupConfirmationFingerprint": prediction.get("lineup_confirmation_fingerprint"),
    }
    provenance = {
        "sourceName": prediction.get("source_name"),
        "sourceFile": prediction.get("source_file"),
        "sourceSha256": prediction.get("source_sha256"),
        "providerFixtureId": prediction.get("provider_fixture_id"),
        "liveFeatureSources": prediction.get("live_feature_sources", []),
        "modelTrainingCutoff": prediction.get("model_training_cutoff"),
        "cutoffAt": prediction.get("freeze_cutoff_at"),
        "modelLockSha256": digest,
    }
    return features, provenance


def _ordered_stage_predictions(
    predictions: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not predictions:
        raise ValueError("at least one prediction stage is required")
    if any(not isinstance(prediction, Mapping) for prediction in predictions):
        raise ValueError("prediction stages must be objects")
    ordered = sorted(
        predictions,
        key=lambda prediction: SUPPORTED_STAGES.index(
            str(prediction.get("freeze_stage"))
        )
        if str(prediction.get("freeze_stage")) in SUPPORTED_STAGES
        else len(SUPPORTED_STAGES),
    )
    stages = [str(prediction.get("freeze_stage") or "") for prediction in ordered]
    if any(stage not in SUPPORTED_STAGES for stage in stages):
        raise ValueError("stages must be supported")
    if len(stages) != len(set(stages)):
        raise ValueError("stages must be unique")
    model_version = str(ordered[0].get("model_version") or "")
    if not model_version or any(
        str(prediction.get("model_version") or "") != model_version
        for prediction in ordered
    ):
        raise ValueError("all stages must use one model version")
    return ordered


def build_registration_payload(
    predictions: list[Mapping[str, Any]],
    fixture_context: Mapping[str, Any],
    *,
    model_version_sha256: str | None = None,
    legacy_espn_authorized: bool = False,
) -> dict[str, Any]:
    """Build one fixture registration with an immutable snapshot per stage."""

    ordered = _ordered_stage_predictions(predictions)
    base = ordered[0]
    competition_id = str(base.get("competition_id") or "")
    name, country = COMPETITIONS.get(competition_id, (competition_id, "unknown"))
    identity = fixture_publication_identity(
        fixture_context,
        competition_id=competition_id,
        legacy_espn_authorized=legacy_espn_authorized,
        use_case=UseCase.MODEL_INPUT,
    )
    provider = identity["provider"]
    provider_fixture_id = identity["fixture_id"]
    home_provider_id = identity["home_team_id"]
    away_provider_id = identity["away_team_id"]
    kickoff_at = _iso(fixture_context.get("kickoff_at"), "fixture.kickoff_at")
    digest = _normalized_model_lock_sha(model_version_sha256)
    if digest is None:
        raise ValueError("model_version_sha256 is required for v260 publication")
    fixture_source = fixture_context.get("source")
    if not isinstance(fixture_source, Mapping):
        raise ValueError("fixture source provenance is required")
    fixture_source_sha = fixture_source.get("raw_sha256")
    if not isinstance(fixture_source_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", fixture_source_sha
    ):
        raise ValueError("prediction primary source hash does not match fixture source")
    primary_policy_source_id = _require_feature_source(fixture_source)
    primary_material_source = {
        "name": identity["fact_provider"],
        "url": str(fixture_source.get("url") or ""),
    }
    kickoff = datetime.fromisoformat(kickoff_at.replace("Z", "+00:00"))
    snapshots: list[dict[str, Any]] = []
    previous_cutoff: datetime | None = None
    previous_generated: datetime | None = None
    for prediction in ordered:
        if str(prediction.get("competition_id") or "") != competition_id:
            raise ValueError("forecast_stage_fixture_identity_mismatch")
        if str(prediction.get("fixture_id") or "") != provider_fixture_id:
            raise ValueError("forecast_stage_fixture_identity_mismatch")
        if prediction.get("source_name") != identity["fact_provider"]:
            raise ValueError("prediction primary source does not match fixture source")
        if prediction.get("source_sha256") != fixture_source_sha:
            raise ValueError("prediction primary source hash does not match fixture source")
        cutoff_at = _iso(prediction.get("freeze_cutoff_at"), "freeze_cutoff_at")
        generated_at = _iso(
            prediction.get("prediction_observed_at") or prediction.get("as_of"),
            "prediction_observed_at",
        )
        cutoff = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if cutoff >= kickoff:
            raise ValueError("freeze cutoff must be before kickoff")
        if generated < cutoff:
            raise ValueError("prediction observation must not be before freeze cutoff")
        if generated > kickoff:
            raise ValueError("prediction observation must be before kickoff")
        if (
            previous_cutoff is not None
            and previous_generated is not None
            and (cutoff <= previous_cutoff or generated <= previous_generated)
        ):
            raise ValueError("forecast_stage_times_must_advance")
        previous_cutoff = cutoff
        previous_generated = generated

        material_sources = _require_prediction_feature_rights(prediction)
        canonical_sources = {
            (str(source["name"]), str(source["url"])): {
                "name": str(source["name"]),
                "url": str(source["url"]),
            }
            for source in [primary_material_source, *material_sources]
        }
        features, provenance = _safe_feature_payload(
            prediction,
            model_version_sha256=digest,
        )
        provenance.update(
            {
                "sourceName": identity["fact_provider"],
                "sourceUrl": fixture_source.get("url"),
                "sourcePolicyId": primary_policy_source_id,
                "sourceRawSha256": fixture_source_sha.lower(),
                "materialSources": [
                    canonical_sources[key] for key in sorted(canonical_sources)
                ],
                "providerFixtureId": provider_fixture_id,
                "identityProvider": provider,
                "identityKind": identity["identity_kind"],
                "cutoffAt": cutoff_at,
                "modelLockSha256": digest,
            }
        )
        snapshots.append(
            {
                "freezeStage": str(prediction["freeze_stage"]),
                "cutoffAt": cutoff_at,
                "generatedAt": generated_at,
                "schemaVersion": publication_schema_version(digest),
                "features": features,
                "provenance": provenance,
            }
        )
    observed_at = snapshots[-1]["generatedAt"]
    return {
        "observedAt": observed_at,
        "competition": {
            "code": competition_id,
            "name": name,
            "country": country,
            "season": str(base.get("season") or fixture_context.get("season") or "unknown"),
        },
        "homeTeam": {
            "canonicalName": str(
                identity.get("home_canonical_name")
                or fixture_context.get("home_team")
                or base.get("home_team")
                or ""
            ),
            "country": fixture_context.get("venue", {}).get("country") if isinstance(fixture_context.get("venue"), Mapping) else None,
            "provider": provider,
            "providerId": str(home_provider_id),
            "identityKind": identity["identity_kind"],
            "confidence": 1.0,
        },
        "awayTeam": {
            "canonicalName": str(
                identity.get("away_canonical_name")
                or fixture_context.get("away_team")
                or base.get("away_team")
                or ""
            ),
            "country": fixture_context.get("venue", {}).get("country") if isinstance(fixture_context.get("venue"), Mapping) else None,
            "provider": provider,
            "providerId": str(away_provider_id),
            "identityKind": identity["identity_kind"],
            "confidence": 1.0,
        },
        "fixture": {
            "provider": provider,
            "providerFixtureId": provider_fixture_id,
            "identityKind": identity["identity_kind"],
            "kickoffAt": kickoff_at,
            "venue": (fixture_context.get("venue") or {}).get("name") if isinstance(fixture_context.get("venue"), Mapping) else None,
            "status": str(fixture_context.get("status") or "scheduled"),
        },
        "featureSnapshots": snapshots,
    }


def build_forecast_payload(
    rows: list[Mapping[str, Any]],
    *,
    fixture_id: int,
    feature_snapshot_ids: Mapping[str, int],
    feature_snapshots: list[Mapping[str, Any]],
    model_version_sha256: str | None = None,
) -> dict[str, Any]:
    """Bind every immutable forecast stage to its own registered snapshot."""

    ordered = _ordered_stage_predictions(rows)
    model_version = str(ordered[0].get("model_version") or "")
    digest = _normalized_model_lock_sha(model_version_sha256)
    if digest is None:
        raise ValueError("model_version_sha256 is required for v260 publication")
    snapshots_by_stage: dict[str, Mapping[str, Any]] = {}
    for snapshot in feature_snapshots:
        if not isinstance(snapshot, Mapping):
            raise ValueError("forecast feature snapshot is malformed")
        stage = str(snapshot.get("freezeStage") or "")
        if stage not in SUPPORTED_STAGES or stage in snapshots_by_stage:
            raise ValueError("forecast feature snapshot stages must be unique and supported")
        snapshots_by_stage[stage] = snapshot
    expected_stages = {str(prediction["freeze_stage"]) for prediction in ordered}
    if set(snapshots_by_stage) != expected_stages or set(feature_snapshot_ids) != expected_stages:
        raise ValueError("forecast_feature_snapshot_stage_map_mismatch")
    snapshot_ids = list(feature_snapshot_ids.values())
    if (
        any(
            not isinstance(snapshot_id, int)
            or isinstance(snapshot_id, bool)
            or snapshot_id <= 0
            for snapshot_id in snapshot_ids
        )
        or len(snapshot_ids) != len(set(snapshot_ids))
    ):
        raise ValueError("forecast_feature_snapshot_id_map_invalid")
    stages: list[dict[str, Any]] = []
    for prediction in ordered:
        stage = str(prediction.get("freeze_stage") or "")
        coverage, missing = _stage_coverage(prediction)
        cutoff_at = _iso(prediction.get("freeze_cutoff_at"), "freeze_cutoff_at")
        locked_at = _iso(
            prediction.get("prediction_observed_at") or prediction.get("as_of"),
            "prediction_observed_at",
        )
        snapshot = snapshots_by_stage[stage]
        features = snapshot.get("features")
        provenance = snapshot.get("provenance")
        if not isinstance(features, Mapping) or not isinstance(provenance, Mapping):
            raise ValueError("forecast feature snapshot is malformed")
        if (
            snapshot.get("cutoffAt") != cutoff_at
            or snapshot.get("generatedAt") != locked_at
            or provenance.get("cutoffAt") != cutoff_at
        ):
            raise ValueError("forecast_stage_time_mismatch")
        if (
            features.get("modelVersion")
            != publication_model_version(model_version, digest)
            or features.get("modelLockSha256") != digest
            or provenance.get("modelLockSha256") != digest
        ):
            raise ValueError("forecast_stage_model_lock_mismatch")
        if (
            features.get("featureCoverage") != coverage
            or features.get("missingFeatures") != missing
        ):
            raise ValueError("forecast_stage_coverage_mismatch")
        expected_goals = _expected_goals(prediction.get("expected_goals"))
        market = _market_probability(prediction)
        if features.get("expectedGoals") != prediction.get("expected_goals"):
            raise ValueError("forecast_stage_expected_goals_mismatch")
        if features.get("marketProbability") != prediction.get("market_probability"):
            raise ValueError("forecast_stage_market_probability_mismatch")
        raw_material_sources = provenance.get("materialSources")
        if not isinstance(raw_material_sources, list) or not raw_material_sources:
            raise ValueError("forecast material sources are missing")
        normalized_material_sources: dict[tuple[str, str], dict[str, str]] = {}
        for source in raw_material_sources:
            if not isinstance(source, Mapping):
                raise ValueError("forecast material source is malformed")
            _require_feature_source(source)
            name = source.get("name")
            url = source.get("url")
            if not isinstance(name, str) or not isinstance(url, str) or not url:
                raise ValueError("forecast material source is malformed")
            normalized_material_sources[(name, url)] = {"name": name, "url": url}
        canonical_material_sources = [
            normalized_material_sources[key]
            for key in sorted(normalized_material_sources)
        ]
        if canonical_material_sources != raw_material_sources:
            raise ValueError("forecast material sources must be sorted and unique")
        primary_name = provenance.get("sourceName")
        primary_url = provenance.get("sourceUrl")
        if not isinstance(primary_name, str) or not isinstance(primary_url, str):
            raise ValueError("forecast_stage_material_sources_mismatch")
        expected_material_sources = {
            (primary_name, primary_url): {
                "name": primary_name,
                "url": primary_url,
            }
        }
        for source in _require_prediction_feature_rights(prediction):
            expected_material_sources[(source["name"], source["url"])] = source
        if canonical_material_sources != [
            expected_material_sources[key]
            for key in sorted(expected_material_sources)
        ]:
            raise ValueError("forecast_stage_material_sources_mismatch")
        stage_probability = _probability(prediction)
        scoreline_matrix = _scoreline_matrix(prediction.get("scoreline_matrix"))
        _validate_stage_distributions(prediction, scoreline_matrix)
        stages.append({
            "stage": stage,
            "featureSnapshotId": feature_snapshot_ids[stage],
            "cutoffAt": cutoff_at,
            "lockedAt": locked_at,
            "featureCoverage": coverage,
            "missingFeatures": missing,
            "expectedHomeGoals": expected_goals["home"],
            "expectedAwayGoals": expected_goals["away"],
            "marketHomeWin": market["home"] if market else None,
            "marketDraw": market["draw"] if market else None,
            "marketAwayWin": market["away"] if market else None,
            "provenance": {"materialSources": canonical_material_sources},
            "probabilities": {
                "oneXTwo": stage_probability,
                "totalGoals": prediction.get("total_goals_probability"),
                "halfFull": prediction.get("half_full_probability"),
                "handicap": prediction.get("handicap_probability"),
                "handicapLine": prediction.get("handicap_line"),
                "market": _market_probability(prediction),
                "marketDelta": prediction.get("probability_delta_model_minus_market"),
            },
            "scorelineMatrix": scoreline_matrix,
        })
    return {
        "fixtureId": fixture_id,
        "modelVersion": publication_model_version(model_version, digest),
        "modelLockSha256": digest,
        "state": "research_only",
        "stages": stages,
    }


def load_archive_rows(path: Path) -> list[ArchiveRow]:
    rows: list[ArchiveRow] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"archive line {line_number} is invalid JSON") from exc
        prediction = record.get("prediction") if isinstance(record, Mapping) else None
        if record.get("conflict") is True or not isinstance(prediction, Mapping):
            continue
        if str(prediction.get("freeze_stage")) not in SUPPORTED_STAGES:
            continue
        rows.append(ArchiveRow(record=record, prediction=prediction))
    return rows


def select_latest_stages(
    rows: list[ArchiveRow],
    *,
    limit: int | None = None,
    model_version: str | None = None,
    model_version_sha256: str | None = None,
) -> list[list[ArchiveRow]]:
    """Select the latest stage rows within one immutable model-lock identity.

    The human-readable model name can remain stable while the locked model
    file digest changes.  Treating those rows as one group would mix two
    prospective windows and make the D1 immutable feature snapshot appear to
    conflict.  When a lock digest is supplied, rows from older locks are
    excluded before stage selection; without it, the digest remains part of
    the grouping key so callers still cannot mix identities accidentally.
    """

    grouped: dict[tuple[str, str, str], dict[str, ArchiveRow]] = defaultdict(dict)
    for row in rows:
        if model_version and str(row.prediction.get("model_version")) != model_version:
            continue
        row_lock_sha = str(
            row.record.get("model_version_sha256")
            or row.prediction.get("model_version_sha256")
            or ""
        )
        if model_version_sha256 and row_lock_sha != model_version_sha256:
            continue
        key = (
            str(row.prediction.get("fixture_id")),
            str(row.prediction.get("model_version")),
            row_lock_sha,
        )
        stage = str(row.prediction.get("freeze_stage"))
        previous = grouped[key].get(stage)
        previous_at = str(previous.record.get("captured_at")) if previous else ""
        current_at = str(row.record.get("captured_at"))
        if previous is None or current_at > previous_at:
            grouped[key][stage] = row
    groups = [list(value.values()) for value in grouped.values()]
    groups.sort(key=lambda group: min(str(item.prediction.get("kickoff_at")) for item in group))
    return groups[:limit] if limit is not None else groups


def publish_payload(
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    token: str,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    producer_attestation: ProducerAttestationContext | None = None,
) -> dict[str, Any]:
    """POST a forecast payload with the same bounded transport policy as ingest."""

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("forecast payload exceeds 512 KiB")
    return _publish_resilient_payload(
        dict(payload),
        endpoint=endpoint,
        token=token,
        timeout=timeout,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        sleep=sleep,
        error_label="forecast",
        producer_attestation=producer_attestation,
    )


def _completed_forecast_receipt_is_valid(
    receipt: Mapping[str, Any],
    *,
    registration: Mapping[str, Any],
    forecast_template: Mapping[str, Any],
) -> bool:
    fixture_id = receipt.get("actualFixtureId")
    feature_snapshot_ids = receipt.get("actualFeatureSnapshotIds")
    response = receipt.get("response")
    if (
        not isinstance(fixture_id, int)
        or isinstance(fixture_id, bool)
        or fixture_id <= 0
        or not isinstance(feature_snapshot_ids, Mapping)
        or not isinstance(response, Mapping)
    ):
        return False
    registration_response = response.get("registration")
    forecast_response = response.get("forecast")
    stages = forecast_template.get("stages")
    if (
        not isinstance(registration_response, Mapping)
        or not isinstance(forecast_response, Mapping)
        or not isinstance(stages, list)
        or receipt.get("rowCount") != len(stages)
        or registration_response.get("fixtureId") != fixture_id
        or registration_response.get("featureSnapshotIds") != feature_snapshot_ids
    ):
        return False
    try:
        _, validated_snapshot_ids = _validate_registration_response(
            registration_response,
            expected_stages=[str(stage.get("stage") or "") for stage in stages],
        )
        _validate_forecast_response(forecast_response, stage_count=len(stages))
    except ValueError:
        return False
    if dict(feature_snapshot_ids) != validated_snapshot_ids:
        return False
    response_digest = receipt.get("responseSha256")
    if response_digest != _sha256(_canonical_json(response).encode("utf-8")):
        return False
    try:
        forecast = _bind_feature_snapshot_ids(
            forecast_template,
            fixture_id=fixture_id,
            feature_snapshot_ids=validated_snapshot_ids,
        )
    except ValueError:
        return False
    actual_outbound = {"registration": dict(registration), "forecast": forecast}
    return receipt.get("actualOutboundPayloadSha256") == _sha256(
        _canonical_json(actual_outbound).encode("utf-8")
    )


def _validate_registration_response(
    response: Mapping[str, Any],
    *,
    expected_stages: list[str],
) -> tuple[int, dict[str, int]]:
    fixture_id = response.get("fixtureId")
    raw_snapshot_ids = response.get("featureSnapshotIds")
    if (
        response.get("status") != "ok"
        or not isinstance(fixture_id, int)
        or isinstance(fixture_id, bool)
        or fixture_id <= 0
        or not isinstance(raw_snapshot_ids, Mapping)
        or not isinstance(response.get("created"), bool)
    ):
        raise ValueError("forecast registration acknowledgement mismatch")
    if set(raw_snapshot_ids) != set(expected_stages):
        raise ValueError("forecast registration acknowledgement stage map mismatch")
    snapshot_ids: dict[str, int] = {}
    for stage in expected_stages:
        snapshot_id = raw_snapshot_ids.get(stage)
        if (
            not isinstance(snapshot_id, int)
            or isinstance(snapshot_id, bool)
            or snapshot_id <= 0
        ):
            raise ValueError("forecast registration acknowledgement stage map mismatch")
        snapshot_ids[stage] = snapshot_id
    if len(set(snapshot_ids.values())) != len(snapshot_ids):
        raise ValueError("forecast registration acknowledgement stage map mismatch")
    return fixture_id, snapshot_ids


def _bind_feature_snapshot_ids(
    forecast_template: Mapping[str, Any],
    *,
    fixture_id: int,
    feature_snapshot_ids: Mapping[str, int],
) -> dict[str, Any]:
    stages = forecast_template.get("stages")
    if not isinstance(stages, list):
        raise ValueError("forecast stages are malformed")
    expected_stages = [str(stage.get("stage") or "") for stage in stages if isinstance(stage, Mapping)]
    if len(expected_stages) != len(stages) or set(feature_snapshot_ids) != set(
        expected_stages
    ):
        raise ValueError("forecast feature snapshot stage map mismatch")
    bound_stages: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            raise ValueError("forecast stages are malformed")
        stage_name = str(stage.get("stage") or "")
        snapshot_id = feature_snapshot_ids[stage_name]
        if (
            not isinstance(snapshot_id, int)
            or isinstance(snapshot_id, bool)
            or snapshot_id <= 0
        ):
            raise ValueError("forecast feature snapshot stage map mismatch")
        bound_stages.append({**stage, "featureSnapshotId": snapshot_id})
    return {**forecast_template, "fixtureId": fixture_id, "stages": bound_stages}


def _validate_forecast_response(
    response: Mapping[str, Any],
    *,
    stage_count: int,
) -> None:
    prediction_id = response.get("predictionId")
    created = response.get("created")
    stages_created = response.get("stagesCreated")
    stages_skipped = response.get("stagesSkipped")
    if (
        response.get("status") != "ok"
        or not isinstance(prediction_id, int)
        or isinstance(prediction_id, bool)
        or prediction_id <= 0
        or not isinstance(created, bool)
        or not isinstance(stages_created, int)
        or isinstance(stages_created, bool)
        or stages_created < 0
        or not isinstance(stages_skipped, int)
        or isinstance(stages_skipped, bool)
        or stages_skipped < 0
        or stages_created + stages_skipped != stage_count
    ):
        raise ValueError("forecast response acknowledgement mismatch")


def require_prediction_archive_identity(
    archive: Path,
    cycle_evidence_path: Path | None,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Re-read the cycle/evaluation archive binding before forecast writes."""

    if dry_run:
        return {"status": "audit_only"}
    if cycle_evidence_path is None:
        raise ValueError("cycle_evidence_path is required outside dry-run")
    # Lazy import avoids the module-level publish_cycle -> publish_forecast
    # dependency becoming circular.
    from league_platform.publish_cycle import _archive_publication_identity

    decision = _archive_publication_identity(cycle_evidence_path, archive)
    if decision.get("status") != "ok":
        raise ValueError(
            str(decision.get("reason", "prediction_archive_identity_invalid"))
        )
    return decision


def publish_archive(
    archive: Path,
    live_path: Path,
    *,
    register_endpoint: str,
    forecast_endpoint: str,
    token: str,
    limit: int | None = None,
    model_version: str | None = None,
    model_version_sha256: str | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    cycle_evidence_path: Path | None = None,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> dict[str, Any]:
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    require_prediction_archive_identity(
        archive,
        cycle_evidence_path,
        dry_run=dry_run,
    )
    snapshot = json.loads(live_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping):
        raise ValueError("current snapshot root must be an object")
    serving_decision = require_fixture_serving(
        snapshot,
        use_case=UseCase.MODEL_INPUT,
    )
    legacy_espn_authorized = (
        serving_decision.get("reason")
        == "legacy_espn_serving_explicitly_authorized"
    )
    fixture_index = _fixture_index(snapshot)
    groups = select_latest_stages(
        load_archive_rows(archive),
        limit=limit,
        model_version=model_version,
        model_version_sha256=model_version_sha256,
    )
    receipt_store = None if dry_run else PublicationReceiptStore(receipts_path or archive.with_name(f"{archive.name}.forecast-receipts.jsonl"))
    completed = receipt_store.completed_records() if receipt_store else {}
    result: dict[str, Any] = {
        "groups": len(groups),
        "stageRows": sum(len(group) for group in groups),
        "published": 0,
        "skipped": [],
        "responses": [],
        "requests": 0,
        "receiptsSkipped": 0,
    }
    prepared: list[dict[str, Any]] = []
    for group in groups:
        prediction = group[0].prediction
        fixture_id = str(prediction.get("fixture_id"))
        context = fixture_index.get(fixture_id)
        if context is None:
            result["skipped"].append({"fixtureId": fixture_id, "reason": "fixture_not_in_current_snapshot"})
            continue
        try:
            group_digests: set[str] = set()
            group_missing_digest = False
            for row in group:
                record_digest = row.record.get("model_version_sha256")
                prediction_digest = row.prediction.get("model_version_sha256")
                if record_digest not in (None, "") and prediction_digest not in (None, "") and str(record_digest).lower() != str(prediction_digest).lower():
                    raise ValueError("mixed_model_lock_identity")
                row_digest = _normalized_model_lock_sha(
                    record_digest if record_digest not in (None, "") else prediction_digest
                )
                if row_digest is None:
                    group_missing_digest = True
                else:
                    group_digests.add(row_digest)
            if len(group_digests) > 1 or (group_digests and group_missing_digest):
                raise ValueError("mixed_model_lock_identity")
            group_lock_sha = next(iter(group_digests), None)
            if group_lock_sha is None:
                raise ValueError("forecast_model_lock_missing")
            registration = build_registration_payload(
                [row.prediction for row in group],
                context,
                model_version_sha256=group_lock_sha,
                legacy_espn_authorized=legacy_espn_authorized,
            )
            raw_feature_snapshots = registration.get("featureSnapshots")
            if not isinstance(raw_feature_snapshots, list):
                raise ValueError("forecast feature snapshots are missing")
            source_policy_ids: set[str] = set()
            placeholder_snapshot_ids: dict[str, int] = {}
            for index, stage_snapshot in enumerate(raw_feature_snapshots, start=1):
                if not isinstance(stage_snapshot, Mapping):
                    raise ValueError("forecast feature snapshot is malformed")
                stage = str(stage_snapshot.get("freezeStage") or "")
                if stage not in SUPPORTED_STAGES or stage in placeholder_snapshot_ids:
                    raise ValueError("forecast feature snapshot stages are malformed")
                placeholder_snapshot_ids[stage] = index
                candidate_provenance = stage_snapshot.get("provenance", {})
                if not isinstance(candidate_provenance, Mapping):
                    raise ValueError("forecast stage provenance is malformed")
                source_policy_id = candidate_provenance.get("sourcePolicyId")
                if not isinstance(source_policy_id, str) or not source_policy_id:
                    raise ValueError("forecast source policy identity is missing")
                source_policy_ids.add(source_policy_id)
                raw_material_sources = candidate_provenance.get("materialSources")
                if not isinstance(raw_material_sources, list):
                    raise ValueError("forecast material sources are missing")
                candidate_policy_ids: set[str] = set()
                for source in raw_material_sources:
                    if not isinstance(source, Mapping):
                        raise ValueError("forecast material source is malformed")
                    name = source.get("name")
                    url = source.get("url")
                    if not isinstance(name, str) or not isinstance(url, str):
                        raise ValueError("forecast material source is malformed")
                    candidate_policy_ids.add(_require_feature_source(source))
                if source_policy_id not in candidate_policy_ids:
                    raise ValueError("forecast source policy identity is inconsistent")
                source_policy_ids.update(candidate_policy_ids)
            # Validate the complete immutable forecast group before the
            # registration endpoint can create either a fixture or feature
            # snapshot.  D1 IDs do not exist yet, so positive placeholders
            # exercise the payload contract and are replaced only after a
            # successful registration response.
            forecast_template = build_forecast_payload(
                [row.prediction for row in group],
                fixture_id=1,
                feature_snapshot_ids=placeholder_snapshot_ids,
                feature_snapshots=raw_feature_snapshots,
                model_version_sha256=group_lock_sha,
            )
            group_digest = _sha256("\n".join(
                _canonical_json(row.prediction)
                for row in sorted(group, key=lambda item: (str(item.prediction.get("freeze_cutoff_at")), str(item.prediction.get("freeze_stage"))))
            ).encode("utf-8"))
            receipt_identity = publication_receipt_identity(
                stream="forecast",
                destinations={
                    "register": register_endpoint,
                    "forecast": forecast_endpoint,
                },
                publication_epoch_value=epoch,
                publication_gate_sha256=str(maturity["receiptSha256"]),
                outbound_payload={
                    "registration": registration,
                    "forecastTemplate": forecast_template,
                },
            )
            registration_attestation = build_producer_attestation_context(
                maturity_context=maturity_context,
                maturity_verification=maturity,
                stream="forecast_registration",
                rights_use_case="model_input",
                source_policy_ids=source_policy_ids,
                publication_epoch=epoch,
                dry_run=dry_run,
            )
            prediction_attestation = build_producer_attestation_context(
                maturity_context=maturity_context,
                maturity_verification=maturity,
                stream="forecast_prediction",
                rights_use_case="model_input",
                source_policy_ids=source_policy_ids,
                publication_epoch=epoch,
                dry_run=dry_run,
            )
            receipt_key = (
                group_digest,
                str(receipt_identity["receiptIdentitySha256"]),
            )
            completed_receipt = completed.get(receipt_key)
            if completed_receipt is not None:
                if not _completed_forecast_receipt_is_valid(
                    completed_receipt,
                    registration=registration,
                    forecast_template=forecast_template,
                ):
                    raise ValueError("forecast_completed_receipt_invalid")
                result["receiptsSkipped"] += 1
                continue
            prepared.append({
                "fixtureId": fixture_id,
                "group": group,
                "registration": registration,
                "forecastTemplate": forecast_template,
                "groupDigest": group_digest,
                "receiptIdentity": receipt_identity,
                "registrationAttestation": registration_attestation,
                "predictionAttestation": prediction_attestation,
            })
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            result["skipped"].append({"fixtureId": fixture_id, "reason": str(exc)})

    # A later invalid/unlicensed group must never be discovered after an
    # earlier group has already mutated D1.  Build and validate the complete
    # selected archive tail first; any preflight failure blocks the whole run.
    if result["skipped"]:
        return result

    if openfootball_raw_archive_dir is None:
        result["rawProvenanceStatus"] = require_openfootball_raw_producer_receipt(
            snapshot,
            dry_run=dry_run,
        )
    else:
        result["rawProvenanceStatus"] = require_openfootball_raw_producer_receipt(
            snapshot,
            dry_run=dry_run,
            openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        )

    for plan in prepared:
        fixture_id = str(plan["fixtureId"])
        group = plan["group"]
        registration = plan["registration"]
        forecast_template = plan["forecastTemplate"]
        group_digest = str(plan["groupDigest"])
        receipt_identity = plan["receiptIdentity"]
        registration_attestation = plan["registrationAttestation"]
        prediction_attestation = plan["predictionAttestation"]
        if not isinstance(group, list) or not isinstance(registration, Mapping) or not isinstance(forecast_template, Mapping) or not isinstance(receipt_identity, Mapping):
            raise AssertionError("prepared forecast group is malformed")
        if dry_run:
            result["responses"].append({"fixtureId": fixture_id, "registration": registration, "stages": len(group)})
            result["published"] += 1
            continue
        try:
            result["requests"] += 1
            registered = publish_payload(
                registration,
                endpoint=register_endpoint,
                token=token,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep=sleep,
                producer_attestation=registration_attestation,
            )
            forecast_stages = forecast_template.get("stages")
            if not isinstance(forecast_stages, list):
                raise ValueError("forecast stages are malformed")
            registered_fixture_id, registered_snapshot_ids = _validate_registration_response(
                registered,
                expected_stages=[
                    str(stage.get("stage") or "")
                    for stage in forecast_stages
                    if isinstance(stage, Mapping)
                ],
            )
            forecast = _bind_feature_snapshot_ids(
                forecast_template,
                fixture_id=registered_fixture_id,
                feature_snapshot_ids=registered_snapshot_ids,
            )
            result["requests"] += 1
            published = publish_payload(
                forecast,
                endpoint=forecast_endpoint,
                token=token,
                timeout=timeout,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep=sleep,
                producer_attestation=prediction_attestation,
            )
            _validate_forecast_response(published, stage_count=len(group))
            if receipt_store is None:
                raise AssertionError("receipt store unavailable outside dry-run")
            response_summary = {
                "registration": {key: registered.get(key) for key in ("status", "fixtureId", "featureSnapshotIds", "created") if key in registered},
                "forecast": {key: published.get(key) for key in ("status", "predictionId", "created", "stagesCreated", "stagesSkipped") if key in published},
            }
            actual_outbound = {"registration": registration, "forecast": forecast}
            receipt_store.append_completed({
                **receipt_identity,
                "fixtureId": fixture_id,
                "stageCount": len(group),
                "rowCount": len(group),
                "batchDigest": group_digest,
                "payloadSha256": receipt_identity["outboundPayloadSha256"],
                "actualFixtureId": forecast["fixtureId"],
                "actualFeatureSnapshotIds": registered_snapshot_ids,
                "actualOutboundPayloadSha256": _sha256(
                    _canonical_json(actual_outbound).encode("utf-8")
                ),
                "responseSha256": _sha256(
                    _canonical_json(response_summary).encode("utf-8")
                ),
                "completedAt": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "response": response_summary,
            })
            result["responses"].append({"fixtureId": fixture_id, "registered": registered, "published": published})
            result["published"] += 1
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            result["skipped"].append({"fixtureId": fixture_id, "reason": str(exc)})
            # Network ambiguity can only be recovered through the endpoint's
            # idempotency contract. Stop rather than writing later groups.
            break
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root_default = (
        Path(os.environ["MATCHLINE_RUNTIME_DIR"])
        if os.environ.get("MATCHLINE_RUNTIME_DIR")
        else None
    )
    parser.add_argument("archive", type=Path, nargs="?", default=Path("data/live/prospective_predictions.jsonl"))
    parser.add_argument("--live-path", type=Path, default=Path("data/live/current.json"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model-version")
    parser.add_argument("--lock", type=Path, default=Path("docs/evidence/prospective-model-lock-current.json"))
    parser.add_argument(
        "--cycle-evidence",
        type=Path,
        default=Path("docs/evidence/prospective-cycle-latest.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publication-epoch",
        default=os.environ.get("MATCHLINE_PUBLICATION_EPOCH"),
    )
    parser.add_argument("--max-snapshot-age-seconds", type=int)
    parser.add_argument("--maturity-receipt", type=Path)
    parser.add_argument("--maturity-runtime-root", type=Path, default=runtime_root_default)
    parser.add_argument("--maturity-platform-verification", type=Path)
    parser.add_argument("--maturity-strict-report", type=Path)
    parser.add_argument("--maturity-evaluation", type=Path)
    parser.add_argument("--maturity-offline-snapshot", type=Path)
    parser.add_argument("--maturity-sites-build-evidence", type=Path)
    parser.add_argument("--maturity-test-evidence", type=Path)
    parser.add_argument("--maturity-publication-audit", type=Path)
    parser.add_argument("--maturity-repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-base-seconds", type=float, default=1.0)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        help="durable OpenFootball raw archive required for non-dry-run publication",
    )
    args = parser.parse_args(argv)
    register_endpoint = os.environ.get("MATCHLINE_FORECAST_REGISTER_URL", "")
    forecast_endpoint = os.environ.get("MATCHLINE_FORECAST_URL", "")
    token = os.environ.get("MATCHLINE_INGEST_TOKEN", "")
    # Import lazily to avoid the module-level publish_cycle ->
    # publish_forecast dependency becoming circular.  The standalone command
    # must enforce exactly the same lock/cycle/snapshot identity as the
    # orchestrated Sites publisher; ``--model-version`` is only an assertion,
    # never an override around that identity.
    from league_platform.publish_cycle import (
        _prospective_publication_identity,
        _snapshot_freshness,
        _snapshot_publication,
    )

    if not args.dry_run and args.max_snapshot_age_seconds is None:
        print("publish blocked: max_snapshot_age_seconds is required", file=sys.stderr)
        return 1

    locked, lock_decision = _prospective_publication_identity(
        args.lock,
        args.cycle_evidence,
        snapshot_path=args.live_path,
        max_age_seconds=args.max_snapshot_age_seconds,
    )
    if locked is None:
        print(
            f"publish blocked: {lock_decision.get('reason', 'prospective_identity_invalid')}",
            file=sys.stderr,
        )
        return 1
    if not args.dry_run:
        freshness = _snapshot_freshness(
            args.live_path,
            args.max_snapshot_age_seconds,
        )
        publication = _snapshot_publication(args.live_path, required=True)
        if freshness.get("status") != "ok" or publication.get("status") != "ok":
            print(
                "publish blocked: current snapshot freshness/publication evidence invalid",
                file=sys.stderr,
            )
            return 1
    model_version = str(locked["freeze_model_name"])
    model_version_sha256 = str(locked["model_version_sha256"])
    if args.model_version is not None and args.model_version != model_version:
        print("publish blocked: model_version does not match prospective lock", file=sys.stderr)
        return 1
    if not args.dry_run and (not register_endpoint or not forecast_endpoint or not token):
        print("publish failed: forecast endpoints and MATCHLINE_INGEST_TOKEN are required", file=sys.stderr)
        return 1
    try:
        maturity_context = None
        if (
            args.maturity_receipt is not None
            and args.maturity_runtime_root is not None
            and args.maturity_platform_verification is not None
            and args.maturity_strict_report is not None
            and args.maturity_evaluation is not None
            and args.maturity_offline_snapshot is not None
            and args.maturity_sites_build_evidence is not None
            and args.maturity_test_evidence is not None
            and args.maturity_publication_audit is not None
        ):
            maturity_context = build_publication_maturity_context(
                receipt_path=args.maturity_receipt,
                platform_verification_path=args.maturity_platform_verification,
                strict_report_path=args.maturity_strict_report,
                prospective_lock_path=args.lock,
                cycle_path=args.cycle_evidence,
                evaluation_path=args.maturity_evaluation,
                live_snapshot_path=args.live_path,
                offline_snapshot_path=args.maturity_offline_snapshot,
                sites_build_evidence_path=args.maturity_sites_build_evidence,
                test_evidence_path=args.maturity_test_evidence,
                publication_audit_path=args.maturity_publication_audit,
                prediction_archive_path=args.archive,
                publication_diagnostic_path=args.live_path.with_name(
                    "current.publication.json"
                ),
                runtime_root=args.maturity_runtime_root,
                repository_root=args.maturity_repository_root,
            )
        elif not args.dry_run:
            raise ValueError("complete maturity artifact paths are required")
        result = publish_archive(
            args.archive,
            args.live_path,
            register_endpoint=register_endpoint or "https://invalid.example/register",
            forecast_endpoint=forecast_endpoint or "https://invalid.example/forecast",
            token=token,
            limit=args.limit,
            model_version=model_version,
            model_version_sha256=model_version_sha256,
            dry_run=args.dry_run,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            receipts_path=args.receipts,
            publication_epoch=args.publication_epoch,
            maturity_context=maturity_context,
            cycle_evidence_path=args.cycle_evidence,
            openfootball_raw_archive_dir=args.openfootball_raw_archive_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if not result["skipped"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_forecast_payload",
    "build_registration_payload",
    "load_archive_rows",
    "publication_model_version",
    "publication_schema_version",
    "publish_archive",
    "publish_payload",
    "select_latest_stages",
]
