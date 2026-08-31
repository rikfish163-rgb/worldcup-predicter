from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_facts_refresh_is_independent_from_model_and_uploads_only_after_success() -> None:
    service = (SYSTEMD / "matchline-facts-snapshot-refresh.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "league_platform.facts_snapshot_refresh" in service
    assert "--output ${MATCHLINE_RUNTIME_DIR}/current.json" in service
    assert "--openfootball-raw-archive-dir ${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}" in service
    assert "--cycle-lock ${MATCHLINE_FACTS_CYCLE_LOCK}" in service
    assert "OnSuccess=matchline-raw-artifact-upload.service" in service
    assert "OnSuccess=matchline-sites-sync-facts.service" in service
    assert "prospective-model-lock" not in service
    assert "--lock" not in service
    assert "--prediction" not in service
    assert "--strict" not in service
    assert "/dev/shm/matchline-live-runtime/openfootball-raw" not in service


def test_facts_refresh_timer_is_persistent_and_waits_between_runs() -> None:
    timer = (SYSTEMD / "matchline-facts-snapshot-refresh.timer").read_text(encoding="utf-8")
    assert "OnBootSec=6min" in timer
    assert "OnUnitInactiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-facts-snapshot-refresh.service" in timer
