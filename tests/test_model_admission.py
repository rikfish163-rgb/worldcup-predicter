from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
)
from league_platform.runtime_paths import resolve_runtime_paths
from league_platform.source_rights import Decision, SourceId


SOURCE_ID = "openfootball:football.json:2026-27:en.1"
SECOND_SOURCE_ID = "openfootball:football.json:2026-27:en.2"
OBSERVED_AT = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)


def _api() -> ModuleType:
    try:
        return importlib.import_module("league_platform.model_admission")
    except ModuleNotFoundError as exc:
        pytest.fail(f"model admission API is missing: {exc}")


def _payload(
    score: list[int] | None = None,
    *,
    blocked_overlay: bool = False,
) -> bytes:
    match: dict[str, Any] = {
        "date": "2026-08-30",
        "time": "16:00",
        "team1": "Arsenal FC",
        "team2": "Coventry City FC",
    }
    if score is not None:
        match["score"] = score
    if blocked_overlay:
        match.update(
            {
                "current_features": {
                    "weather": {"temperature_c": 20.0},
                    "official_lineup": {"confirmed": True},
                    "market": {"home_probability": 0.99},
                },
                "model_eligible": True,
                "trusted": True,
                "authorization_reference": "self-issued",
            }
        )
    return json.dumps({"matches": [match]}, separators=(",", ":")).encode()


def _observation(
    source_id: str,
    payload: bytes,
    *,
    observed_at: datetime,
) -> OpenFootballRawObservation:
    config = OPENFOOTBALL_SOURCES[source_id]
    return OpenFootballRawObservation(
        source_id=source_id,
        url=config["url"],
        retrieved_at=observed_at,
        payload=payload,
        source_format=config["format"],
        competition_id=config["competition_id"],
        season=config["season"],
        timezone_name=config["timezone"],
        license="CC0-1.0",
    )


def _store(
    archive_root: Path,
    *,
    observed_at: datetime = OBSERVED_AT,
    score: list[int] | None = None,
    source_id: str = SOURCE_ID,
    payload: bytes | None = None,
) -> dict[str, Any]:
    return OpenFootballRawArchive(archive_root).store(
        _observation(
            source_id,
            payload if payload is not None else _payload(score),
            observed_at=observed_at,
        )
    )


def test_verified_corpus_comes_only_from_raw_replay_and_v260_rights(tmp_path: Path) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    receipt = _store(archive_root, score=[2, 1])

    corpus = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    assert isinstance(corpus, api.VerifiedOpenFootballCorpus)
    assert corpus.schema_version == "matchline.verified_openfootball_corpus.v1"
    assert corpus.policy_version == "v260"
    assert corpus.observed_before == OBSERVED_AT
    assert corpus.source_ids == (SOURCE_ID,)
    assert corpus.rows[0]["score"] == {"home": 2, "away": 1}
    assert corpus.selected_observations[0].record_sha256 == receipt["record_sha256"]
    assert corpus.selected_observations[0].raw_sha256 == receipt["raw_sha256"]
    assert [evidence.use_case.value for evidence in corpus.rights_evidence] == [
        "training",
        "model_input",
    ]
    assert all(evidence.decision.is_allowed for evidence in corpus.rights_evidence)
    assert all(evidence.decision.network_opened is False for evidence in corpus.rights_evidence)
    for digest in (
        corpus.admission_sha256,
        corpus.source_manifest_sha256,
        corpus.parser_contract_sha256,
        corpus.rows_sha256,
        corpus.identity_sha256,
    ):
        assert len(digest) == 64


def test_corpus_and_every_nested_raw_row_are_immutable(tmp_path: Path) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    _store(archive_root)
    corpus = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    with pytest.raises(TypeError):
        corpus.rows[0]["home_team"] = "Forged FC"
    with pytest.raises(TypeError):
        corpus.rows[0]["source"]["raw_sha256"] = "0" * 64
    with pytest.raises((AttributeError, TypeError)):
        corpus.rows = ()

    assert corpus.rows[0]["home_team"] == "Arsenal FC"
    assert corpus.rows[0]["source"]["raw_sha256"] != "0" * 64


def test_self_reported_trust_cannot_construct_or_uplift_a_corpus(tmp_path: Path) -> None:
    api = _api()
    forged = {
        "status": "verified_raw_candidate",
        "training_admitted": True,
        "trusted": True,
        "authorization_reference": "self-issued",
        "rows": [{"id": "forged", "model_eligible": True}],
        "admission_sha256": "f" * 64,
    }

    with pytest.raises(TypeError, match="load_verified_openfootball_corpus"):
        api.VerifiedOpenFootballCorpus(**forged)
    with pytest.raises(TypeError):
        api.load_verified_openfootball_corpus(
            tmp_path / "missing",
            source_ids=[SOURCE_ID],
            observed_before=OBSERVED_AT,
            snapshot_receipt=forged,
        )


def test_cutoff_is_causal_and_future_appends_do_not_rewrite_identity(tmp_path: Path) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    first_at = OBSERVED_AT
    selected_at = OBSERVED_AT + timedelta(hours=1)
    future_at = OBSERVED_AT + timedelta(hours=2)
    _store(archive_root, observed_at=first_at)
    _store(archive_root, observed_at=selected_at, score=[2, 1])

    first = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=first_at,
    )
    selected = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=selected_at,
    )
    repeated = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=selected_at,
    )

    assert first.rows[0]["score"] is None
    assert selected.rows[0]["score"] == {"home": 2, "away": 1}
    assert first.identity_sha256 != selected.identity_sha256
    assert repeated == selected

    _store(archive_root, observed_at=future_at, score=[3, 1])
    replayed = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=selected_at,
    )
    later_cutoff_same_row = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=selected_at + timedelta(seconds=1),
    )

    assert replayed == selected
    assert later_cutoff_same_row.rows == selected.rows
    assert later_cutoff_same_row.identity_sha256 != selected.identity_sha256


def test_runtime_paths_is_the_only_structured_archive_root_input(tmp_path: Path) -> None:
    api = _api()
    runtime_paths = resolve_runtime_paths(tmp_path / "runtime")
    _store(runtime_paths.openfootball_raw_archive_dir)

    direct = api.load_verified_openfootball_corpus(
        runtime_paths.openfootball_raw_archive_dir,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )
    resolved = api.load_verified_openfootball_corpus(
        runtime_paths,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    assert resolved == direct
    with pytest.raises(api.ModelAdmissionBlocked) as caught:
        api.load_verified_openfootball_corpus(
            {"openfootball_raw_archive_dir": str(runtime_paths.openfootball_raw_archive_dir)},
            source_ids=[SOURCE_ID],
            observed_before=OBSERVED_AT,
        )
    assert caught.value.reason_code == "invalid_archive_root"


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_raw_evidence_fails_closed(tmp_path: Path, failure: str) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    if failure == "corrupt":
        receipt = _store(archive_root)
        raw_path = archive_root / receipt["raw_path"]
        raw_path.write_bytes(b"X" * raw_path.stat().st_size)

    with pytest.raises(api.ModelAdmissionBlocked) as caught:
        api.load_verified_openfootball_corpus(
            archive_root,
            source_ids=[SOURCE_ID],
            observed_before=OBSERVED_AT,
        )

    assert caught.value.reason_code == "raw_archive_verification_failed"
    assert caught.value.rights_decision is None


def test_verified_archive_with_no_rows_fails_closed(tmp_path: Path) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    _store(archive_root, payload=b'{"matches":[]}')

    with pytest.raises(api.ModelAdmissionBlocked) as caught:
        api.load_verified_openfootball_corpus(
            archive_root,
            source_ids=[SOURCE_ID],
            observed_before=OBSERVED_AT,
        )

    assert caught.value.reason_code == "verified_corpus_empty"
    assert caught.value.rights_decision is None


@pytest.mark.parametrize(
    "source_id, expected_status",
    [
        (SourceId.OPENLIGADB_SECONDARY_RESULTS.value, "verified_odbl_isolated"),
        (SourceId.CFL_OFFICIAL_CURRENT.value, "blocked_rights_not_verified"),
        ("openfootball:csl:2026:unregistered", "unknown_source_fail_closed"),
    ],
)
def test_non_openfootball_training_sources_fail_at_central_rights(
    tmp_path: Path,
    source_id: str,
    expected_status: str,
) -> None:
    api = _api()

    with pytest.raises(api.ModelAdmissionBlocked) as caught:
        api.load_verified_openfootball_corpus(
            tmp_path / "unused",
            source_ids=[source_id],
            observed_before=OBSERVED_AT,
        )

    assert caught.value.reason_code == "source_rights_blocked"
    assert caught.value.rights_decision is not None
    assert caught.value.rights_decision.source_id == source_id
    assert caught.value.rights_decision.use_case == "training"
    assert caught.value.rights_decision.rights_status == expected_status
    assert caught.value.rights_decision.is_allowed is False


def test_blocked_snapshot_fields_cannot_enter_corpus_rows(tmp_path: Path) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    _store(archive_root, payload=_payload(blocked_overlay=True))

    corpus = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    row = corpus.rows[0]
    assert {
        "current_features",
        "weather",
        "official_lineup",
        "market",
        "model_eligible",
        "trusted",
        "authorization_reference",
    }.isdisjoint(row)


def test_identity_binds_source_selection_and_central_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()
    archive_root = tmp_path / "archive"
    _store(archive_root)
    _store(
        archive_root,
        source_id=SECOND_SOURCE_ID,
        payload=b'{"matches":[]}',
    )
    one_source = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )
    two_sources = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID, SECOND_SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    assert one_source.rows == two_sources.rows
    assert one_source.identity_sha256 != two_sources.identity_sha256

    real_require = api.require_source_rights

    def revised_policy(source_id: object, use_case: object) -> Decision:
        decision = real_require(source_id, use_case)
        return replace(decision, reason=f"{decision.reason} Test-only policy revision.")

    monkeypatch.setattr(api, "require_source_rights", revised_policy)
    revised = api.load_verified_openfootball_corpus(
        archive_root,
        source_ids=[SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    assert revised.rows == one_source.rows
    assert revised.admission_sha256 == one_source.admission_sha256
    assert revised.identity_sha256 != one_source.identity_sha256
