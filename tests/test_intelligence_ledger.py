from datetime import datetime, timezone
import json
from multiprocessing import get_context
from pathlib import Path

import pytest

from league_platform.intelligence import (
    ObservationLedger,
    coverage_level,
    observation_from_payload,
    recover_observation_fragments,
    resolve_observations,
)


AS_OF = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def _observation(*, source_tier="reliable_media", payload=None, published=True, enters=True, entity_id="fixture-1"):
    return observation_from_payload(
        entity_type="fixture",
        entity_id=entity_id,
        kind="lineup",
        payload=payload or {"confirmed": True},
        source_name="Example source",
        source_url="https://example.com/lineup",
        source_tier=source_tier,
        observed_at=AS_OF,
        published_at=AS_OF if published else None,
        confidence=0.9,
        enters_model=enters,
    )


def _append_worker(path: str, start: int) -> None:
    ledger = ObservationLedger(Path(path))
    rows = [
        _observation(entity_id=f"fixture-{start + offset}")
        for offset in range(20)
    ]
    ledger.append(rows)


def test_ledger_is_idempotent_and_append_only(tmp_path):
    ledger = ObservationLedger(tmp_path / "observations.jsonl")
    item = _observation()
    assert ledger.append([item, item]) == {"accepted": 1, "skipped_duplicate": 1}
    assert ledger.append([item]) == {"accepted": 0, "skipped_duplicate": 1}
    assert ledger.read() == [item]
    assert ledger.index_path.is_file()


def test_ledger_reuses_digest_index_without_rescanning_history(tmp_path, monkeypatch):
    ledger = ObservationLedger(tmp_path / "observations.jsonl")
    first = _observation(entity_id="fixture-1")
    second = _observation(entity_id="fixture-2")
    assert ledger.append([first]) == {"accepted": 1, "skipped_duplicate": 0}

    def fail_rebuild():
        raise AssertionError("digest index was unexpectedly rebuilt")

    monkeypatch.setattr(ledger, "_rebuild_index", fail_rebuild)
    assert ledger.append([first, second]) == {"accepted": 1, "skipped_duplicate": 1}


def test_ledger_does_not_drop_distinct_entities_with_same_raw_payload(tmp_path):
    ledger = ObservationLedger(tmp_path / "observations.jsonl")
    first = _observation(entity_id="fixture-1")
    second = _observation(entity_id="fixture-2")
    assert first.raw_hash == second.raw_hash
    assert ledger.append([first, second]) == {"accepted": 2, "skipped_duplicate": 0}
    assert {row.entity_id for row in ledger.read()} == {"fixture-1", "fixture-2"}


def test_ledger_serializes_concurrent_appends_without_interleaving(tmp_path):
    path = tmp_path / "observations.jsonl"
    context = get_context("fork")
    processes = [
        context.Process(target=_append_worker, args=(str(path), index * 20))
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    rows = ObservationLedger(path).read()
    assert len(rows) == 80
    assert len({row.entity_id for row in rows}) == 80


def test_ledger_read_exposes_malformed_history_without_rewriting_it(tmp_path):
    path = tmp_path / "observations.jsonl"
    item = _observation()
    path.write_text(
        json.dumps(item.as_dict(), ensure_ascii=False) + "\n{bad-json\n",
        encoding="utf-8",
    )
    ledger = ObservationLedger(path)

    assert ledger.read() == [item]
    assert ledger.last_read_errors[0]["line"] == 2
    with pytest.raises(ValueError, match="invalid observation ledger line 2"):
        ledger.read(strict=True)


def test_recovery_finds_complete_record_after_truncated_prefix():
    broken = _observation(entity_id="fixture-broken").as_dict()
    recovered = _observation(entity_id="fixture-recovered").as_dict()
    line = (
        json.dumps(broken, ensure_ascii=False)[:-17]
        + json.dumps(recovered, ensure_ascii=False)
    )

    fragments = recover_observation_fragments(line)

    assert len(fragments) == 1
    assert fragments[0]["observation"]["entity_id"] == "fixture-recovered"
    assert fragments[0]["offset"] == len(json.dumps(broken, ensure_ascii=False)) - 17


def test_resolver_prefers_official_source_but_keeps_conflict():
    media = _observation(source_tier="reliable_media", payload={"confirmed": False})
    official = _observation(source_tier="official", payload={"confirmed": True})
    resolved = resolve_observations([media, official], cutoff_at=AS_OF)
    winner = resolved[("fixture", "fixture-1", "lineup")]
    assert winner["observation"]["source_tier"] == "official"
    assert winner["conflict"] is True
    assert winner["critical_conflict"] is True
    assert winner["usable_for_model"] is False


def test_future_observation_is_not_visible_at_cutoff():
    future = _observation()
    assert resolve_observations([future], cutoff_at="2026-08-11T11:59:00+00:00") == {}


def test_coverage_level_exposes_missing_features():
    coverage = coverage_level(
        expected_features={"lineup", "injury", "weather", "market"},
        available_features={"lineup", "market"},
    )
    assert coverage["level"] == "medium"
    assert coverage["missing"] == ["injury", "weather"]


def test_high_coverage_requires_every_declared_feature_to_be_present():
    expected = {"history", "xg", "market", "weather", "injuries"}
    incomplete = coverage_level(
        expected_features=expected,
        available_features=expected - {"injuries"},
    )
    complete = coverage_level(
        expected_features=expected,
        available_features=expected,
    )

    assert incomplete["ratio"] == 0.8
    assert incomplete["missing"] == ["injuries"]
    assert incomplete["level"] == "medium"
    assert complete["level"] == "high"


def test_resolver_exposes_repeated_confirmation_count():
    first = _observation(payload={"confirmed": True})
    second = observation_from_payload(
        entity_type="fixture",
        entity_id="fixture-1",
        kind="lineup",
        payload={"confirmed": True},
        source_name="Another source",
        source_url="https://another.example/lineup",
        source_tier="reliable_media",
        observed_at=AS_OF,
        published_at=AS_OF,
        confidence=0.7,
        enters_model=True,
    )
    resolved = resolve_observations([first, second], cutoff_at=AS_OF)
    assert resolved[("fixture", "fixture-1", "lineup")]["confirmation_count"] == 2


def test_resolver_blocks_conflicting_official_lineup_from_model():
    first = observation_from_payload(
        entity_type="fixture",
        entity_id="fixture-1",
        kind="official_lineup",
        payload={"home": ["player-a"], "away": ["player-b"]},
        source_name="Premier League official",
        source_url="https://example.com/official-lineup",
        source_tier="official",
        observed_at=AS_OF,
        published_at=AS_OF,
        confidence=0.99,
        enters_model=True,
    )
    second = observation_from_payload(
        entity_type="fixture",
        entity_id="fixture-1",
        kind="official_lineup",
        payload={"home": ["player-c"], "away": ["player-b"]},
        source_name="Premier League official",
        source_url="https://example.com/official-lineup",
        source_tier="official",
        observed_at=AS_OF,
        published_at=AS_OF,
        confidence=0.99,
        enters_model=True,
    )

    resolved = resolve_observations([first, second], cutoff_at=AS_OF)
    result = resolved[("fixture", "fixture-1", "official_lineup")]
    assert result["conflict"] is True
    assert result["critical_conflict"] is True
    assert result["usable_for_model"] is False


def test_resolver_quarantines_unresolved_noncritical_market_conflict():
    first = observation_from_payload(
        entity_type="fixture",
        entity_id="fixture-1",
        kind="market_1x2",
        payload={"home": 0.5, "draw": 0.25, "away": 0.25},
        source_name="Market A",
        source_url="https://market-a.example/fixture-1",
        source_tier="authorized",
        observed_at=AS_OF,
        published_at=AS_OF,
        confidence=0.9,
        enters_model=True,
    )
    second = observation_from_payload(
        entity_type="fixture",
        entity_id="fixture-1",
        kind="market_1x2",
        payload={"home": 0.25, "draw": 0.25, "away": 0.5},
        source_name="Market B",
        source_url="https://market-b.example/fixture-1",
        source_tier="authorized",
        observed_at=AS_OF,
        published_at=AS_OF,
        confidence=0.9,
        enters_model=True,
    )

    result = resolve_observations([first, second], cutoff_at=AS_OF)[
        ("fixture", "fixture-1", "market_1x2")
    ]

    assert result["conflict"] is True
    assert result["unresolved_conflict"] is True
    assert result["critical_conflict"] is False
    assert result["usable_for_model"] is False
