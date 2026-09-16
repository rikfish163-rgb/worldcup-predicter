from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_research_sync_unit_replaces_duplicate_upload_sequence() -> None:
    service = (SYSTEMD / "matchline-sites-sync-research.service").read_text(encoding="utf-8")
    assert "OnSuccess=matchline-sites-sync-private.service" in service
    assert "EnvironmentFile=%h/.config/matchline/matchline-sites.env" in service
    assert "league_platform.publish_sites_sync" in service
    assert "--lane research_audit" in service
    assert "--mode incremental" in service
    assert "--raw-endpoint ${MATCHLINE_RAW_ARTIFACT_ENDPOINT}" in service
    assert "--research-endpoint ${MATCHLINE_RESEARCH_FIXTURE_ENDPOINT}" in service
    assert "--audit-endpoint ${MATCHLINE_PROSPECTIVE_AUDIT_ENDPOINT}" in service
    assert "--structured-endpoint ${MATCHLINE_ARTIFACT_ENDPOINT}" in service
    assert (
        "Environment=MATCHLINE_RESEARCH_RECEIPT="
        "/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-runtime/sites-sync-state/research-audit-completed.json"
    ) in service
    assert (
        "Environment=MATCHLINE_RESEARCH_CURSOR="
        "/media/hetaisheng/044A81D94A81C83E/"
        "soccerdata-live-runtime/sites-sync-state/research-audit-cursor.json"
    ) in service
    assert "--receipt ${MATCHLINE_RESEARCH_RECEIPT}" in service
    assert "--cursor ${MATCHLINE_RESEARCH_CURSOR}" in service
    assert "--receipt ${MATCHLINE_SITES_SYNC_STATE_DIR}/research-completed.json" not in service
    assert "--cursor ${MATCHLINE_SITES_SYNC_STATE_DIR}/research-cursor.json" not in service
    runtime_env = "EnvironmentFile=-%h/.config/matchline/matchline-runtime.env"
    assert runtime_env in service
    assert service.index("Environment=MATCHLINE_RESEARCH_RECEIPT=") < service.index(runtime_env)
    assert service.index("Environment=MATCHLINE_RESEARCH_CURSOR=") < service.index(runtime_env)
    assert "MATCHLINE_INGEST_TOKEN" not in service
    assert "MATCHLINE_PRODUCER_SIGNING_SECRET" not in service
    assert "candidate_prediction_archive" not in service


def test_private_sync_unit_has_no_raw_or_formal_upload_lane() -> None:
    service = (SYSTEMD / "matchline-sites-sync-private.service").read_text(encoding="utf-8")
    assert "league_platform.publish_sites_sync" in service
    assert "--lane private_evidence" in service
    assert "--structured-endpoint ${MATCHLINE_ARTIFACT_ENDPOINT}" in service
    assert "--raw-endpoint" not in service
    assert "--research-endpoint" not in service
    assert "--audit-endpoint" not in service
    assert "--lane formal" not in service
    assert "candidate_prediction_archive" not in service


def test_sync_timer_and_env_contract_are_bounded_and_explicit() -> None:
    timer = (SYSTEMD / "matchline-sites-sync-research.timer").read_text(encoding="utf-8")
    environment = (SYSTEMD / "matchline-sites.env.example").read_text(encoding="utf-8")
    runtime = (SYSTEMD / "matchline-runtime.env.example").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-sites-sync-research.service" in timer
    assert "MATCHLINE_PROSPECTIVE_AUDIT_ENDPOINT=https://" in environment
    assert "MATCHLINE_ARTIFACT_ENDPOINT=https://" in environment
    assert "MATCHLINE_SITES_SYNC_STATE_DIR=/media/" in runtime
