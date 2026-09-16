from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_live_overlay_systemd_lane_is_separate_from_forecast_cycle() -> None:
    service = (ROOT / "deploy/systemd/matchline-live-overlay.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/matchline-live-overlay.timer").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 -m league_platform.live_overlay" in service
    assert "live_overlay.json" in service
    assert "--output ${MATCHLINE_RUNTIME_DIR}/live_overlay.json" in service
    assert "--public-output" not in service
    assert "--served-output" not in service
    assert "matchline_sites/public/live_overlay.json" not in service
    assert "matchline_sites/dist/client/live_overlay.json" not in service
    assert "--min-free-bytes=268435456" in service
    assert "current.json" in service
    assert "matchline-prospective-cycle.service" not in service
    assert "OnUnitInactiveSec=60s" in timer
    assert "matchline-live-overlay.service" in timer


def test_live_overlay_service_uses_the_external_runtime_and_bounded_timeout() -> None:
    service = (ROOT / "deploy/systemd/matchline-live-overlay.service").read_text(encoding="utf-8")
    assert "MATCHLINE_RUNTIME_DIR=/dev/shm/matchline-live-runtime" in service
    assert "MATCHLINE_RUNTIME_PERSISTENCE=volatile_tmpfs" in service
    assert "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime" in service
    assert "XDG_CACHE_HOME=/dev/shm/matchline-live-overlay-xdg-cache" in service
    assert "--lookback-minutes 180" in service
    assert "TimeoutStartSec=90s" in service


def test_external_readmodel_lane_writes_only_external_artifacts() -> None:
    service = (ROOT / "deploy/systemd/matchline-runtime-readmodel.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/matchline-runtime-readmodel.timer").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 -m league_platform.build_offline_bundle" in service
    assert "--live-path ${MATCHLINE_RUNTIME_DIR}/current.json" in service
    assert "--strict-report ${MATCHLINE_RUNTIME_DIR}/strict-backtest-current.json" in service
    assert (
        "Environment=MATCHLINE_PROSPECTIVE_LOCK=/home/hetaisheng/soccerdata/docs/evidence/"
        "prospective-model-lock-current.json" in service
    )
    assert "--prospective-lock ${MATCHLINE_PROSPECTIVE_LOCK}" in service
    assert (
        "--runtime-archive-manifest /media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-archive/recovery-snapshots/runtime-archive-manifest.json"
    ) in service
    assert "--compact-output ${MATCHLINE_RUNTIME_DIR}/offline_snapshot.json" in service
    assert "matchline_sites/public/offline_snapshot.json" not in service
    assert "run-matchline-sites-build.sh" not in service
    assert "--compact-js-output ${MATCHLINE_RUNTIME_DIR}/league-platform-offline_data.js" in service
    assert "--output ${MATCHLINE_RUNTIME_DIR}/offline_full.js" in service
    assert "/home/hetaisheng/soccerdata/league_platform/site/offline_data.js" not in service
    assert "--evidence-dir /home/hetaisheng/soccerdata/docs/evidence" in service
    assert "--runtime-evidence-dir ${MATCHLINE_RUNTIME_DIR}" in service
    assert "OnBootSec=10min" in timer
    assert "OnUnitInactiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-runtime-readmodel.service" in timer
