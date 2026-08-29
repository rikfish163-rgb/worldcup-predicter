from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_facts_sync_unit_isolated_from_formal_evidence() -> None:
    service = (SYSTEMD / "matchline-sites-sync-facts.service").read_text(encoding="utf-8")
    assert "league_platform.publish_sites_sync" in service
    assert "--lane research_facts_only" in service
    assert "--mode incremental" in service
    assert "--archive-root ${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}" in service
    assert "--snapshot ${MATCHLINE_RESEARCH_FIXTURE_SNAPSHOT}" in service
    assert "--raw-endpoint ${MATCHLINE_RAW_ARTIFACT_ENDPOINT}" in service
    assert "--research-endpoint ${MATCHLINE_RESEARCH_FIXTURE_ENDPOINT}" in service
    assert "facts-completed.json" in service
    assert "facts-cursor.json" in service
    assert "--lock" not in service
    assert "--cycle" not in service
    assert "--evaluation" not in service
    assert "--strict-report" not in service
    assert "--prediction-archive" not in service
    assert "MATCHLINE_INGEST_TOKEN" not in service
    assert "MATCHLINE_PRODUCER_SIGNING_SECRET" not in service


def test_facts_sync_timer_is_persistent_and_bounded() -> None:
    timer = (SYSTEMD / "matchline-sites-sync-facts.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-sites-sync-facts.service" in timer
