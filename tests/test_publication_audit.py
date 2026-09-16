from __future__ import annotations

from datetime import datetime, timezone

from league_platform.publication_audit import build_publication_audit


def _fixture(*, local_as_of: str, public_as_of: str, local_hash: str = "abc", public_hash: str = "abc"):
    snapshot = {
        "as_of": local_as_of,
        "schema_version": "matchline.live.v1",
        "production_ready": False,
    }
    lock = {"model_version_sha256": local_hash}
    public_status = {
        "status": "offline_snapshot",
        "generatedAt": public_as_of,
        "asOf": public_as_of,
        "productionReady": False,
        "gate": {
            "status": "pending_prospective_window",
            "productionAllowed": False,
            "modelVersionSha256": public_hash,
        },
    }
    return snapshot, lock, public_status


def test_publication_audit_passes_when_lock_and_snapshot_are_current():
    snapshot, lock, public_status = _fixture(
        local_as_of="2026-08-18T09:20:00+00:00",
        public_as_of="2026-08-18T09:10:00+00:00",
    )
    report = build_publication_audit(
        snapshot=snapshot,
        lock=lock,
        public_status=public_status,
        public_sources={"runs": []},
        public_status_request={"status": "ok"},
        public_sources_request={"status": "ok"},
        checked_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
        max_lag_seconds=900,
    )

    assert report["status"] == "pass"
    assert report["errors"] == []
    assert report["checks"]["model_lock_match"] is True
    assert report["checks"]["snapshot_lag_seconds"] == 600
    assert report["deployment_required"] is False


def test_publication_audit_blocks_stale_public_lock_and_snapshot():
    snapshot, lock, public_status = _fixture(
        local_as_of="2026-08-18T09:20:00+00:00",
        public_as_of="2026-08-18T08:00:00+00:00",
        public_hash="old",
    )
    report = build_publication_audit(
        snapshot=snapshot,
        lock=lock,
        public_status=public_status,
        public_status_request={"status": "ok"},
        checked_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
        max_lag_seconds=900,
    )

    assert report["status"] == "blocked"
    assert "public_snapshot_behind_local" in report["errors"]
    assert "model_lock_mismatch" in report["errors"]
    assert report["checks"]["model_lock_match"] is False
    assert report["deployment_required"] is True


def test_publication_audit_blocks_missing_public_payload_and_bad_timestamps():
    report = build_publication_audit(
        snapshot={"as_of": "not-a-time", "production_ready": False},
        lock={"model_version_sha256": "abc"},
        public_status=None,
        public_status_request={"status": "http_error"},
        public_sources_request={"status": "malformed"},
        checked_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )

    assert report["status"] == "blocked"
    assert "public_status:http_error" in report["errors"]
    assert "public_sources:malformed" in report["errors"]
    assert "local_as_of:invalid" in report["errors"]
    assert "public_as_of:invalid" in report["errors"]
    assert report["checks"]["public_read_model_reachable"] is False


def test_publication_audit_rejects_successful_but_malformed_read_models():
    report = build_publication_audit(
        snapshot={"as_of": "2026-08-18T09:20:00+00:00", "production_ready": False},
        lock={"model_version_sha256": "abc"},
        public_status={},
        public_sources={"items": []},
        public_status_request={"status": "ok"},
        public_sources_request={"status": "ok"},
        checked_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )

    assert report["status"] == "blocked"
    assert "public_status:malformed" in report["errors"]
    assert "public_sources:malformed" in report["errors"]


def test_publication_audit_blocks_missing_current_contract_routes():
    snapshot, lock, public_status = _fixture(
        local_as_of="2026-08-18T09:20:00+00:00",
        public_as_of="2026-08-18T09:10:00+00:00",
    )
    report = build_publication_audit(
        snapshot=snapshot,
        lock=lock,
        public_status=public_status,
        public_status_request={"status": "ok"},
        public_preflight={
            "status": "stale_or_incomplete_routes",
            "reachable": True,
            "deployment_ready": False,
            "all_expected_routes_ok": False,
            "routes": [
                {"path": "/api/v1/catalog", "status_code": 404},
                {"path": "/api/v1/matches", "status_code": 200},
            ],
        },
        checked_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
        max_lag_seconds=900,
    )

    assert report["status"] == "blocked"
    assert "public_routes:stale_or_incomplete_routes" in report["errors"]
    assert report["checks"]["public_contract_routes_ok"] is False
    assert report["public_preflight"]["failed_routes"] == ["/api/v1/catalog"]
    assert report["deployment_required"] is True
