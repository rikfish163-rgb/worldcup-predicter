from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_restore_service_is_atomic_fail_closed_boot_gate():
    service = (ROOT / "deploy/systemd/matchline-runtime-restore.service").read_text(
        encoding="utf-8"
    )
    assert "Type=oneshot" in service
    assert "RemainAfterExit=yes" in service
    assert "Before=matchline-prospective-cycle-runtime-only.service" in service
    assert "matchline-live-overlay.service" in service
    assert "matchline-runtime-readmodel.service" in service
    assert "ExecStart=/usr/bin/python3 -m league_platform.runtime_restore" in service
    assert (
        "--manifest=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-archive/"
        "recovery-snapshots/runtime-archive-manifest.json"
    ) in service
    assert (
        "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-archive/recovery-snapshots"
    ) in service
    assert "--destination=${MATCHLINE_RUNTIME_DIR}" in service
    assert "--quarantine-incomplete-target" in service
    assert "--min-free-bytes=536870912" in service
    assert (
        "Environment=MATCHLINE_CHECKPOINT_DIR=/media/hetaisheng/044A81D94A81C83E/"
        "matchline-runtime-critical-checkpoints"
    ) in service
    assert "--checkpoint-root=${MATCHLINE_CHECKPOINT_DIR}" in service


def test_runtime_consumers_require_successful_restore_gate():
    for name in (
        "matchline-prospective-cycle-runtime-only.service",
        "matchline-live-overlay.service",
        "matchline-runtime-readmodel.service",
    ):
        service = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "Requires=matchline-runtime-restore.service" in service
        assert "After=" in service
        assert "matchline-runtime-restore.service" in service
