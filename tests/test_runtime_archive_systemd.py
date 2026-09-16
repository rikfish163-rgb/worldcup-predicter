from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_archive_service_is_recovery_only_and_has_bounded_storage_guard():
    service = (ROOT / "deploy/systemd/matchline-runtime-archive.service").read_text(encoding="utf-8")
    readmodel = (ROOT / "deploy/systemd/matchline-runtime-readmodel.service").read_text(
        encoding="utf-8"
    )
    assert "Type=oneshot" in service
    assert "ExecCondition=/usr/bin/test -f ${MATCHLINE_RUNTIME_DIR}/current.json" in service
    assert "Environment=MATCHLINE_RUNTIME_DIR=/dev/shm/matchline-live-runtime" in service
    assert (
        "Environment=MATCHLINE_ARCHIVE_DIR=/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-archive/recovery-snapshots"
    ) in service
    assert (
        "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-archive/recovery-snapshots"
    ) in service
    assert "ExecStart=/usr/bin/python3 -m league_platform.runtime_archive" in service
    assert "--runtime-dir=${MATCHLINE_RUNTIME_DIR}" in service
    assert "--archive-dir=${MATCHLINE_ARCHIVE_DIR}" in service
    assert "--min-free-bytes=536870912" in service
    assert "--keep=1" in service
    assert "ExecStartPost=/usr/bin/python3 -m league_platform.runtime_archive" in service
    assert "--verify" in service
    assert (
        "--verification-receipt=${MATCHLINE_ARCHIVE_DIR}/"
        "runtime-archive-verification.json"
    ) in service
    assert "restore" in service
    assert "D1" not in service
    assert "model" in service
    assert "Before=matchline-runtime-readmodel.service" in service
    assert "matchline-runtime-archive.service" in next(
        line for line in readmodel.splitlines() if line.startswith("After=")
    )


def test_runtime_archive_timer_runs_after_boot_and_is_persistent():
    timer = (ROOT / "deploy/systemd/matchline-runtime-archive.timer").read_text(encoding="utf-8")
    assert "OnBootSec=20min" in timer
    assert "OnUnitInactiveSec=60min" in timer
    assert "AccuracySec=30s" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-runtime-archive.service" in timer
