import json

from league_platform.intelligence import observation_from_payload
from league_platform.intelligence_repair import quarantine_ledger


def _row(entity_id: str) -> dict:
    return observation_from_payload(
        entity_type="fixture",
        entity_id=entity_id,
        kind="lineup",
        payload={"confirmed": True},
        source_name="Example source",
        source_url="https://example.com/lineup",
        source_tier="reliable_media",
        observed_at="2026-08-11T12:00:00+00:00",
        published_at="2026-08-11T12:00:00+00:00",
        confidence=0.9,
        enters_model=True,
    ).as_dict()


def test_quarantine_is_append_only_and_records_recoverable_fragment(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    quarantine = tmp_path / "observations.quarantine.jsonl"
    first = json.dumps(_row("broken"), ensure_ascii=False)
    second = json.dumps(_row("recoverable"), ensure_ascii=False)
    ledger.write_text(first[:-11] + second + "\n", encoding="utf-8")
    original = ledger.read_bytes()

    result = quarantine_ledger(ledger, quarantine, write=True)

    assert result["status"] == "quarantined"
    assert result["malformed_lines"] == 1
    assert result["recoverable_records"] == 1
    assert result["appended"] == 1
    assert ledger.read_bytes() == original
    entry = json.loads(quarantine.read_text(encoding="utf-8").splitlines()[0])
    assert entry["line"] == 1
    assert entry["model_use"] == "excluded"
    assert entry["recovered_records"][0]["entity_id"] == "recoverable"

    second_run = quarantine_ledger(ledger, quarantine, write=True)
    assert second_run["appended"] == 0
    assert second_run["skipped_existing"] == 1
