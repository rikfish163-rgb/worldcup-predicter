from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_sites_preview_is_managed_and_reads_the_runtime_overlay() -> None:
    service = (ROOT / "deploy/systemd/matchline-sites-local.service").read_text(
        encoding="utf-8"
    )

    assert "Type=simple" in service
    assert "WorkingDirectory=/home/hetaisheng/soccerdata/matchline_sites" in service
    assert (
        "ExecStart=/usr/bin/node /home/hetaisheng/soccerdata/matchline_sites/"
        "node_modules/vinext/dist/cli.js start --hostname 127.0.0.1 --port 3000"
    ) in service
    assert (
        "Environment=MATCHLINE_LIVE_OVERLAY_PATH=/dev/shm/"
        "matchline-live-runtime/live_overlay.json"
    ) in service
    assert "Environment=MATCHLINE_LOCAL_STATE_DIR=/dev/shm/matchline-sites-local-state" in service
    assert "Requires=matchline-runtime-restore.service" in service
    assert (
        "ConditionPathExists=/home/hetaisheng/soccerdata/matchline_sites/"
        "app/offline_snapshot.data.json"
    ) in service
    assert (
        "ConditionPathExists=/home/hetaisheng/soccerdata/matchline_sites/"
        "dist/server/index.js"
    ) in service
    assert (
        "ExecCondition=/usr/bin/grep -Fq static_snapshot_not_public "
        "/home/hetaisheng/soccerdata/matchline_sites/dist/server/index.js"
    ) in service
    assert "dist/client/offline_snapshot.json" not in service
    assert "matchline_sites/public/offline_snapshot.json" not in service
    assert "Restart=on-failure" in service
    assert "RestartSec=5s" in service
    assert "EnvironmentFile=" not in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "PrivateDevices=true" in service
    assert "BindReadOnlyPaths=/dev/shm/matchline-live-runtime" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "CapabilityBoundingSet=" in service
    assert "RestrictNamespaces=true" in service
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in service
    assert "UMask=0077" in service


def test_readmodel_refresh_does_not_rebuild_or_restart_the_source_checkout() -> None:
    service = (ROOT / "deploy/systemd/matchline-runtime-readmodel.service").read_text(
        encoding="utf-8"
    )
    assert "run-matchline-sites-build.sh" not in service
    assert "matchline-sites-local.service" not in service
    assert "matchline_sites/public" not in service
    assert "matchline_sites/dist" not in service
