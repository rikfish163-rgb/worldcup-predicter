from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
)
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)


OBSERVED_AT = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
PREMIER_LEAGUE_SOURCE_ID = "openfootball:england:2015-16:1-premierleague"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _rehash_candidate_rows(candidate: dict) -> dict:
    candidate["rows_sha256"] = _canonical_sha256(candidate["rows"])
    admission_identity = {
        "schema_version": candidate["schema_version"],
        "observed_before": candidate["observed_before"],
        "source_ids": candidate["source_ids"],
        "selected_record_sha256": [
            record["record_sha256"] for record in candidate["selected_records"]
        ],
        "source_manifest_sha256": candidate["source_manifest_sha256"],
        "parser_contract_sha256": candidate["parser_contract_sha256"],
        "rows_sha256": candidate["rows_sha256"],
    }
    candidate["admission_sha256"] = _canonical_sha256(admission_identity)
    return candidate


def _source_api():
    try:
        return importlib.import_module("league_platform.sources.openfootball_verified")
    except ModuleNotFoundError as exc:
        pytest.fail(f"verified OpenFootball history source is missing: {exc}")


def _store(
    archive_root: Path,
    *,
    source_id: str,
    payload: bytes,
    observed_at: datetime = OBSERVED_AT,
) -> dict:
    config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
    return OpenFootballRawArchive(archive_root).store(
        OpenFootballRawObservation(
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
    )


def test_load_maps_only_verified_finished_raw_row_to_domain_match(tmp_path: Path) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Manchester United  1-0 (1-0)  Tottenham Hotspur
"""
    receipt = _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )
    raw_candidate = load_verified_openfootball_archive(
        archive_root,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        observed_before=OBSERVED_AT,
    )

    source = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )
    result = source.load("premier-league")

    assert result.competition_id == "premier-league"
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.id == raw_candidate["rows"][0]["id"]
    assert match.provider_fixture_id == match.id
    assert match.season == "2015-16"
    assert match.kickoff_at.isoformat() == "2015-08-08T11:45:00+00:00"
    assert match.kickoff_time_quality == "exact"
    assert match.kickoff_time_source == "explicit"
    assert match.home_team == "Manchester United"
    assert match.away_team == "Tottenham Hotspur"
    assert match.home_team_id == "premier-league:man-united"
    assert match.away_team_id == "premier-league:tottenham"
    assert match.status == "finished"
    assert match.score is not None
    assert (match.score.home, match.score.away) == (1, 0)
    assert (match.score.halftime_home, match.score.halftime_away) == (1, 0)
    assert match.source_file == (
        f"raw/sha256/{receipt['raw_sha256'][:2]}/{receipt['raw_sha256']}.raw"
    )
    assert match.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert match.source_name == "OpenFootball"
    assert match.source_license_status == "CC0-1.0"
    assert match.kickoff_time_observed_at is None
    assert match.result_observed_at is None
    assert match.market_probability is None
    assert match.market_opening_probability is None
    assert match.market_closing_probability is None
    assert match.handicap_line is None
    assert match.handicap_probability is None
    assert match.total_line is None
    assert match.total_probability is None
    assert match.total_opening_probability is None
    assert match.total_closing_probability is None


def test_admission_manifest_binds_raw_policy_parser_and_sorted_domain_rows(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Manchester United  1-0 (1-0)  Tottenham Hotspur
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )
    raw_candidate = load_verified_openfootball_archive(
        archive_root,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        observed_before=OBSERVED_AT,
    )
    source = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )

    manifest = source.admission_manifest()

    assert manifest["schema_version"] == ("matchline.openfootball_verified_history_admission.v1")
    assert manifest["status"] == "training_admitted"
    assert manifest["training_admitted"] is True
    assert manifest["training_source"] == "verified_openfootball_raw_archive_only"
    assert manifest["admission_scope"] == "historical_training"
    assert manifest["loader_contract_version"] == "1.0.0"
    assert manifest["adapter_contract_version"] == "1.0.0"
    assert manifest["observed_before"] == "2026-08-25T01:02:03+00:00"
    assert manifest["source_ids"] == [PREMIER_LEAGUE_SOURCE_ID]
    assert manifest["competition_ids"] == ["premier-league"]
    assert manifest["raw_admission_sha256"] == raw_candidate["admission_sha256"]
    assert manifest["raw_manifest_sha256"] == raw_candidate["manifest_sha256"]
    assert manifest["source_manifest_sha256"] == raw_candidate["source_manifest_sha256"]
    assert manifest["parser_contract_sha256"] == raw_candidate["parser_contract_sha256"]
    assert manifest["raw_rows_sha256"] == raw_candidate["rows_sha256"]
    assert manifest["counts"] == {
        "raw_rows": 1,
        "finished_admitted": 1,
        "upcoming_isolated": 0,
        "exact_kickoff_admitted": 1,
        "date_only_admitted": 0,
        "competition_rows": {"premier-league": 1},
    }
    for field in (
        "source_identity_sha256",
        "training_policy_sha256",
        "domain_rows_sha256",
        "admission_sha256",
    ):
        assert len(manifest[field]) == 64
    matches = source.load("premier-league").matches
    assert manifest["domain_rows_sha256"] == _canonical_sha256(
        [match.to_dict() for match in sorted(matches, key=lambda match: match.id)]
    )
    identity = {
        key: value
        for key, value in manifest.items()
        if key not in {"admission_sha256", "raw_manifest_sha256", "statement"}
    }
    assert manifest["admission_sha256"] == _canonical_sha256(identity)
    assert "only" in manifest["statement"].lower()
    assert "training" in manifest["statement"].lower()


def test_date_only_finished_row_uses_local_anchor_and_upcoming_row_is_isolated(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  Manchester United  1-0 (1-0)  Tottenham Hotspur
    Future Home v Future Away
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )

    source = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )
    result = source.load("premier-league")

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.kickoff_at.isoformat() == "2015-08-08T00:00:00+01:00"
    assert match.kickoff_time_quality == "date_only"
    assert match.kickoff_time_source == "missing"
    assert result.quality["date_only_kickoff_rows"] == 1
    assert source.admission_manifest()["counts"] == {
        "raw_rows": 2,
        "finished_admitted": 1,
        "upcoming_isolated": 1,
        "exact_kickoff_admitted": 0,
        "date_only_admitted": 1,
        "competition_rows": {"premier-league": 1},
    }


def test_archive_with_no_finished_score_cannot_mint_training_admission(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
    12:45  Future Home v Future Away
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="no finished matches",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_adapter_rejects_a_loader_row_tampered_to_the_wrong_competition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Manchester United  1-0 (1-0)  Tottenham Hotspur
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )

    def tampered_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["rows"][0]["competition_id"] = "bundesliga"
        return candidate

    monkeypatch.setattr(api, "load_verified_openfootball_archive", tampered_loader)

    with pytest.raises(api.OpenFootballRawArchiveError):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_adapter_recomputes_loader_row_digest_before_training_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Manchester United  1-0 (1-0)  Tottenham Hotspur
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )

    def tampered_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["rows"][0]["home_team"] = "Injected Team"
        return candidate

    monkeypatch.setattr(api, "load_verified_openfootball_archive", tampered_loader)

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="candidate.rows_sha256 does not match rows",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_finished_result_observed_before_its_kickoff_is_rejected_as_future_leakage(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sun Aug 30 2026
  12:45  Future Home  2-1 (1-0)  Future Away
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
        observed_at=OBSERVED_AT,
    )

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="finished result was observed before kickoff",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_six_competition_archive_and_explicit_subset_are_loaded_independently(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    by_competition: dict[str, str] = {}
    for source_id, config in OPENFOOTBALL_HISTORY_SOURCES.items():
        if config["season"] == "2015-16":
            by_competition.setdefault(config["competition_id"], source_id)
    assert set(by_competition) == {
        "premier-league",
        "championship",
        "bundesliga",
        "la-liga",
        "serie-a",
        "ligue-1",
    }
    for index, (competition_id, source_id) in enumerate(sorted(by_competition.items())):
        payload = (
            "= Verified history\n"
            "\u25aa Matchday 1\n"
            "Sat Aug 8 2015\n"
            f"  12:{index:02d}  {competition_id} Home  2-1 (1-0)  "
            f"{competition_id} Away\n"
        ).encode()
        _store(archive_root, source_id=source_id, payload=payload)

    all_source_ids = list(reversed(list(by_competition.values())))
    all_source = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=all_source_ids,
    )

    for competition_id in sorted(by_competition):
        result = all_source.load(competition_id)
        assert len(result.matches) == 1
        assert result.matches[0].competition_id == competition_id
        assert result.matches[0].home_team_id.startswith(f"{competition_id}:")
    all_manifest = all_source.admission_manifest()
    assert all_manifest["source_ids"] == sorted(all_source_ids)
    assert all_manifest["competition_ids"] == sorted(by_competition)
    assert all_manifest["counts"]["finished_admitted"] == 6

    subset_ids = [by_competition["premier-league"], by_competition["bundesliga"]]
    subset = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=subset_ids,
    )
    assert subset.admission_manifest()["competition_ids"] == [
        "bundesliga",
        "premier-league",
    ]
    assert subset.admission_manifest()["counts"]["finished_admitted"] == 2
    with pytest.raises(api.OpenFootballRawArchiveError):
        subset.load("la-liga")


def test_cutoff_selects_latest_observation_and_future_append_keeps_admission_digest(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    first_at = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 8, 25, 2, tzinfo=timezone.utc)
    future_at = datetime(2026, 8, 25, 3, tzinfo=timezone.utc)

    def payload(score: str) -> bytes:
        return (
            "= English Premier League 2015/16\n"
            "\u25aa Matchday 1\n"
            "Sat Aug 8 2015\n"
            f"  12:45  Manchester United  {score} (1-0)  Tottenham Hotspur\n"
        ).encode()

    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload("1-0"),
        observed_at=first_at,
    )
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload("2-0"),
        observed_at=cutoff,
    )
    before = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=cutoff,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )
    before_manifest = before.admission_manifest()
    assert before.load("premier-league").matches[0].score.home == 2

    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload("3-0"),
        observed_at=future_at,
    )
    after = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=cutoff,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )
    after_manifest = after.admission_manifest()

    assert after.load("premier-league").matches[0].score.home == 2
    assert after_manifest["raw_manifest_sha256"] != before_manifest["raw_manifest_sha256"]
    assert after_manifest["raw_admission_sha256"] == before_manifest["raw_admission_sha256"]
    assert after_manifest["domain_rows_sha256"] == before_manifest["domain_rows_sha256"]
    assert after_manifest["admission_sha256"] == before_manifest["admission_sha256"]


def test_domain_row_raw_identity_must_match_the_selected_archive_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Manchester United  1-0 (1-0)  Tottenham Hotspur
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )

    def internally_rehashed_tamper(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["rows"][0]["source"]["raw_sha256"] = "f" * 64
        candidate["rows"][0]["source"]["hash"] = f"sha256:{'f' * 64}"
        candidate["rows"][0]["lineage"]["raw_sha256"] = "f" * 64
        candidate["rows"][0]["lineage"]["hash"] = f"sha256:{'f' * 64}"
        return _rehash_candidate_rows(candidate)

    monkeypatch.setattr(
        api,
        "load_verified_openfootball_archive",
        internally_rehashed_tamper,
    )

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="row raw identity does not match selected record",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_historical_results_update_after_same_kickoff_batch_not_at_archive_time(
    tmp_path: Path,
) -> None:
    from league_platform.model import _elo_predictions
    from league_platform.strict_backtest import _result_event_time

    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = b"""= English Premier League 2015/16
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  Shared Home  1-0 (1-0)  Same Batch One
  12:45  Shared Home  1-0 (1-0)  Same Batch Two
Sun Aug 9 2015
  12:45  Shared Home  1-0 (1-0)  Later Opponent
"""
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )
    source = api.VerifiedOpenFootballHistorySource(
        archive_root,
        observed_before=OBSERVED_AT,
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )
    matches = source.load("premier-league").matches

    assert all(match.result_observed_at is None for match in matches)
    assert all(match.kickoff_time_observed_at is None for match in matches)
    assert all(_result_event_time(match) == match.kickoff_at for match in matches)
    predictions = _elo_predictions(matches, draw_rate=0.25, home_advantage=40.0)
    home_probability = {row[0].away_team: row[1][0] for row in predictions}
    assert home_probability["Same Batch One"] == pytest.approx(home_probability["Same Batch Two"])
    assert home_probability["Later Opponent"] > home_probability["Same Batch One"]


def test_cross_source_duplicate_domain_fixture_id_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    bundesliga_source_id = next(
        source_id
        for source_id, config in OPENFOOTBALL_HISTORY_SOURCES.items()
        if config["competition_id"] == "bundesliga" and config["season"] == "2015-16"
    )
    payloads = {
        PREMIER_LEAGUE_SOURCE_ID: b"""= Premier League
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  English Home  1-0 (1-0)  English Away
""",
        bundesliga_source_id: b"""= Bundesliga
\xe2\x96\xaa Matchday 1
Sat Aug 8 2015
  12:45  German Home  1-0 (1-0)  German Away
""",
    }
    for source_id, payload in payloads.items():
        _store(archive_root, source_id=source_id, payload=payload)

    def duplicate_id_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        duplicate_id = candidate["rows"][0]["id"]
        candidate["rows"][1]["id"] = duplicate_id
        candidate["rows"][1]["provider_fixture_ids"] = {"OpenFootball": duplicate_id}
        return _rehash_candidate_rows(candidate)

    monkeypatch.setattr(api, "load_verified_openfootball_archive", duplicate_id_loader)

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="duplicate domain fixture id",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID, bundesliga_source_id],
        )


def test_constructor_reloads_and_rejects_tampered_content_addressed_raw_bytes(
    tmp_path: Path,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    payload = (
        "= Premier League\n\u25aa Matchday 1\nSat Aug 8 2015\n  12:45  Home  1-0 (1-0)  Away\n"
    ).encode()
    receipt = _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=payload,
    )
    (archive_root / receipt["raw_path"]).write_bytes(b"X" * len(payload))

    with pytest.raises(
        api.OpenFootballRawArchiveError,
        match="raw object hash does not match manifest",
    ):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


@pytest.mark.parametrize(
    "source_ids",
    [
        ["openfootball:unknown:2015-16:league"],
        [OPENFOOTBALL_CURRENT_SOURCE_IDS[0]],
        [PREMIER_LEAGUE_SOURCE_ID, PREMIER_LEAGUE_SOURCE_ID],
    ],
)
def test_constructor_rejects_unknown_current_and_duplicate_source_ids(
    tmp_path: Path,
    source_ids: list[str],
) -> None:
    api = _source_api()

    with pytest.raises(api.OpenFootballRawArchiveError, match="historical|allowlist"):
        api.VerifiedOpenFootballHistorySource(
            tmp_path / "archive",
            observed_before=OBSERVED_AT,
            source_ids=source_ids,
        )


def test_row_source_and_lineage_license_cannot_be_internally_resigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=(
            "= Premier League\n\u25aa Matchday 1\nSat Aug 8 2015\n  12:45  Home  1-0 (1-0)  Away\n"
        ).encode(),
    )

    def internally_resigned_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["rows"][0]["source"]["license"] = "MIT"
        candidate["rows"][0]["lineage"]["license"] = "MIT"
        return _rehash_candidate_rows(candidate)

    monkeypatch.setattr(
        api,
        "load_verified_openfootball_archive",
        internally_resigned_loader,
    )

    with pytest.raises(api.OpenFootballRawArchiveError, match="source provenance"):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_date_only_anchor_must_use_the_allowlisted_source_timezone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=(
            "= Premier League\n\u25aa Matchday 1\nSat Aug 8 2015\n  Home  1-0 (1-0)  Away\n"
        ).encode(),
    )

    def timezone_tampered_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["rows"][0]["kickoff_timezone"] = "UTC"
        return _rehash_candidate_rows(candidate)

    monkeypatch.setattr(
        api,
        "load_verified_openfootball_archive",
        timezone_tampered_loader,
    )

    with pytest.raises(api.OpenFootballRawArchiveError, match="kickoff timezone"):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )


def test_loader_cutoff_must_equal_the_requested_training_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _source_api()
    archive_root = tmp_path / "archive"
    _store(
        archive_root,
        source_id=PREMIER_LEAGUE_SOURCE_ID,
        payload=(
            "= Premier League\n\u25aa Matchday 1\nSat Aug 8 2015\n  12:45  Home  1-0 (1-0)  Away\n"
        ).encode(),
    )

    def cutoff_tampered_loader(*args, **kwargs):
        candidate = load_verified_openfootball_archive(*args, **kwargs)
        candidate["observed_before"] = "2026-08-26T01:02:03+00:00"
        return _rehash_candidate_rows(candidate)

    monkeypatch.setattr(
        api,
        "load_verified_openfootball_archive",
        cutoff_tampered_loader,
    )

    with pytest.raises(api.OpenFootballRawArchiveError, match="cutoff"):
        api.VerifiedOpenFootballHistorySource(
            archive_root,
            observed_before=OBSERVED_AT,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        )
