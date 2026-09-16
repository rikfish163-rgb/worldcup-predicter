from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_raw_artifact_upload_unit_is_independent_from_model_publication() -> None:
    service = (ROOT / "deploy/systemd/matchline-raw-artifact-upload.service").read_text(
        encoding="utf-8"
    )
    assert "ConditionPathExists=%h/.config/matchline/matchline-sites.env" in service
    assert "EnvironmentFile=%h/.config/matchline/matchline-sites.env" in service
    assert "RequiresMountsFor=/media/" in service
    assert "league_platform.publish_raw_artifacts" in service
    assert "--archive-root ${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}" in service
    assert "--endpoint ${MATCHLINE_RAW_ARTIFACT_ENDPOINT}" in service
    assert "league_platform.publish_research_fixtures" in service
    assert "--snapshot ${MATCHLINE_RESEARCH_FIXTURE_SNAPSHOT}" in service
    assert "--endpoint ${MATCHLINE_RESEARCH_FIXTURE_ENDPOINT}" in service
    assert "prospective-model-lock" not in service
    assert "league_platform.publish_cycle" not in service


def test_raw_artifact_timer_is_bounded_and_persistent() -> None:
    timer = (ROOT / "deploy/systemd/matchline-raw-artifact-upload.timer").read_text(
        encoding="utf-8"
    )
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-raw-artifact-upload.service" in timer
