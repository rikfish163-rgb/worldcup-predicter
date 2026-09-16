from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_cycle_triggers_isolated_audit_uploader_after_success() -> None:
    cycle = (ROOT / "deploy/systemd/matchline-prospective-cycle-runtime-only.service").read_text(
        encoding="utf-8"
    )
    assert "OnSuccess=matchline-prospective-audit-upload.service" in cycle

    service = (ROOT / "deploy/systemd/matchline-prospective-audit-upload.service").read_text(
        encoding="utf-8"
    )
    assert "EnvironmentFile=%h/.config/matchline/matchline-sites.env" in service
    assert "EnvironmentFile=-/etc/matchline/matchline-runtime.env" in service
    assert "league_platform.publish_prospective_audit" in service
    assert "--lock ${MATCHLINE_PROSPECTIVE_LOCK}" in service
    assert "--cycle ${MATCHLINE_RUNTIME_DIR}/runtime-only-cycle-latest.json" in service
    assert "--evaluation ${MATCHLINE_RUNTIME_DIR}/runtime-only-evaluation-current.json" in service
    assert "--live-snapshot ${MATCHLINE_RUNTIME_DIR}/current.json" in service
    assert "--prediction-archive ${MATCHLINE_RUNTIME_DIR}/prospective_predictions.jsonl" in service
    assert "--endpoint ${MATCHLINE_PROSPECTIVE_AUDIT_ENDPOINT}" in service
    assert "MATCHLINE_INGEST_TOKEN" not in service
    assert "MATCHLINE_PRODUCER_SIGNING_SECRET" not in service
