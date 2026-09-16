from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_runtime_only_systemd_lane_is_data_only_and_mirrors_runtime_pointers():
    service = (ROOT / "deploy/systemd/matchline-prospective-cycle-runtime-only.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "deploy/systemd/matchline-prospective-cycle-runtime-only.timer").read_text(
        encoding="utf-8"
    )

    assert "Type=oneshot" in service
    assert (
        "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime" in service
    )
    assert "ExecStart=/usr/bin/python3 -m league_platform.prospective_cycle" in service
    assert "--runtime-only" in service
    assert "--runtime-dir=${MATCHLINE_RUNTIME_DIR}" in service
    assert "--openfootball-raw-archive-dir=${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}" in service
    assert "--enable-wikidata" in service
    assert (
        "--crawl4ai-config /home/hetaisheng/soccerdata/config/crawl4ai_operator_public.json"
        in service
    )
    assert (
        "Environment=PYTHONPATH=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/python-deps-no-deps"
        in service
    )
    assert "Environment=XDG_CACHE_HOME=/dev/shm/matchline-runtime-only-xdg-cache" in service
    assert (
        "Environment=MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/media/hetaisheng/"
        "044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw" in service
    )
    assert (
        "Environment=MATCHLINE_PROSPECTIVE_LOCK=/home/hetaisheng/soccerdata/docs/evidence/"
        "prospective-model-lock-current.json" in service
    )
    assert "--lock=${MATCHLINE_PROSPECTIVE_LOCK}" in service
    assert (
        "Environment=MATCHLINE_HISTORY_RUNTIME_ROOT=/media/hetaisheng/"
        "044A81D94A81C83E/soccerdata-live-runtime" in service
    )
    assert (
        "ExecStartPost=/bin/bash /home/hetaisheng/soccerdata/"
        "deploy/systemd/run-matchline-strict-report.sh" in service
    )
    assert "ExecStartPost=/usr/bin/python3 -m league_platform.runtime_evidence" in service
    assert (
        "--lock=${MATCHLINE_PROSPECTIVE_LOCK}"
        in service.split("ExecStartPost=/usr/bin/python3 -m league_platform.runtime_evidence", 1)[
            1
        ]
    )
    assert "--cycle ${MATCHLINE_RUNTIME_DIR}/runtime-only-cycle-latest.json" in service
    assert "--evaluation ${MATCHLINE_RUNTIME_DIR}/runtime-only-evaluation-current.json" in service
    assert "--audit-output-dir ${MATCHLINE_RUNTIME_DIR}" in service
    assert "--mirror-target-dir ${MATCHLINE_RUNTIME_DIR}" in service
    assert (
        "Environment=MATCHLINE_CHECKPOINT_DIR=/media/hetaisheng/044A81D94A81C83E/"
        "matchline-runtime-critical-checkpoints"
    ) in service
    assert "ExecStartPost=/usr/bin/python3 -m league_platform.runtime_checkpoint create" in service
    assert "--runtime-dir=${MATCHLINE_RUNTIME_DIR}" in service
    assert "--checkpoint-root=${MATCHLINE_CHECKPOINT_DIR}" in service
    assert "--retain=4" in service
    assert "D1" in service

    assert "OnBootSec=8min" in timer
    assert "OnUnitInactiveSec=15min" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "OnActiveSec=" not in timer
    assert "Unit=matchline-prospective-cycle-runtime-only.service" in timer


def test_runtime_env_declares_durable_openfootball_raw_archive_outside_tmpfs():
    environment = (ROOT / "deploy/systemd/matchline-runtime.env.example").read_text(
        encoding="utf-8"
    )

    assert (
        "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-runtime/openfootball-raw"
    ) in environment
    assert "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/dev/shm" not in environment
