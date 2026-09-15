from __future__ import annotations

from copy import deepcopy

import pytest

from league_platform.source_research import (
    REQUIRED_SOURCE_FIELDS,
    SCHEMA_VERSION,
    build_source_research_report,
    render_markdown,
    validate_source_research_report,
)


_DIGEST = "0123456789abcdef" * 4


def _snapshot() -> dict:
    return {
        "schema_version": "1.0.0",
        "as_of": "2026-09-16T00:00:00+00:00",
        "source_registry": [
            {
                "id": "openfootball_current",
                "name": "OpenFootball 当前赛程与赛果",
                "adapter": "league_platform.live_sources.openfootball_live",
                "hosts": ["raw.githubusercontent.com"],
                "access_policy": "fixed",
                "runtime": {
                    "status": "fresh",
                    "record_count": 2,
                    "error_count": 0,
                    "errors": [],
                    "raw_sha256": _DIGEST,
                },
            },
            {
                "id": "espn_schedule_summary",
                "name": "ESPN 赛程、赛果与事件摘要",
                "adapter": "league_platform.live_sources.espn",
                "hosts": ["site.api.espn.com"],
                "access_policy": "blocked",
                "runtime": {
                    "status": "rights_blocked",
                    "record_count": 0,
                    "error_count": 0,
                    "errors": [],
                    "network_opened": False,
                },
            },
        ],
        "openfootball_current": {
            "status": "ok",
            "fixtures": [
                {"source": {"raw_sha256": _DIGEST}},
                {"source": {"raw_sha256": "f" * 64}},
            ],
        },
    }


def test_report_covers_registry_and_policy_inventory_without_unknown_values():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="a" * 64,
        snapshot_path="fixture/current.json",
        observed_at="2026-09-16T00:01:00+00:00",
    )

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["source_id_count"] == 47
    assert len(report["sources"]) == 47
    assert sorted(report["source_ids"]) == sorted(row["id"] for row in report["sources"])
    for row in report["sources"]:
        assert set(REQUIRED_SOURCE_FIELDS) <= set(row)
        assert row["observed_http"]["network_opened"] is (row["id"] == "openfootball_current")
        assert "unknown" not in repr({key: row[key] for key in REQUIRED_SOURCE_FIELDS}).casefold()
    validate_source_research_report(report)


def test_allowed_snapshot_and_blocked_snapshot_keep_distinct_http_semantics():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="b" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    by_id = {row["id"]: row for row in report["sources"]}

    openfootball = by_id["openfootball_current"]
    assert openfootball["failure_or_status"]["state"] == "fresh"
    assert openfootball["observed_http"]["state"] == "success"
    assert openfootball["observed_http"]["attempted"] is True
    assert _DIGEST in openfootball["raw_hash_policy"]["observed_hashes"]

    espn = by_id["espn_schedule_summary"]
    assert espn["failure_or_status"]["state"] == "rights_blocked"
    assert espn["observed_http"]["state"] == "not_attempted"
    assert espn["observed_http"]["network_opened"] is False
    assert espn["eligibility"] == {"display": "blocked", "model": "blocked", "publication": "blocked"}


def test_probe_receipt_can_add_real_hash_and_http_parser_evidence():
    probe = {
        "schema_version": "matchline.source_probe_evidence.v1",
        "probes": [
            {
                "source_id": "wikidata_entities",
                "attempted": True,
                "state": "success",
                "status": "ok",
                "network_opened": True,
                "status_codes": [200],
                "content_types": ["application/json"],
                "raw_hashes": [_DIGEST],
                "record_count": 1,
                "error_count": 0,
                "evidence": ["operator_probe:2026-09-15T18:17:21Z"],
            }
        ],
    }
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="c" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
        probe_evidence=probe,
    )
    row = next(item for item in report["sources"] if item["id"] == "wikidata_entities")
    assert row["observed_http"]["state"] == "success"
    assert row["observed_http"]["status_codes"] == [200]
    assert row["raw_hash_policy"]["observed_hashes"] == [_DIGEST]


def test_report_requires_explicit_observed_at():
    with pytest.raises(ValueError, match="observed_at"):
        build_source_research_report(_snapshot(), snapshot_sha256="d" * 64, observed_at="")


def test_validator_rejects_unknown_and_blocked_network_claims():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="e" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    unknown_report = deepcopy(report)
    unknown_report["sources"][0]["rights_basis"]["conclusion"] = "unknown"
    with pytest.raises(ValueError, match="unknown"):
        validate_source_research_report(unknown_report)

    unsafe_report = deepcopy(report)
    blocked = next(row for row in unsafe_report["sources"] if row["id"] == "espn_schedule_summary")
    blocked["observed_http"]["network_opened"] = True
    with pytest.raises(ValueError, match="network"):
        validate_source_research_report(unsafe_report)


def test_markdown_contains_summary_and_per_source_contract():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="f" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    markdown = render_markdown(report)
    assert "逐源抓取可行性审计" in markdown
    assert "openfootball_current" in markdown
    assert "espn_schedule_summary" in markdown
    assert "display" in markdown and "publication" in markdown
def test_alias_runtime_evidence_uses_actual_snapshot_key():
    snapshot = _snapshot()
    snapshot["openligadb"] = {
        "status": "ok",
        "retrieved_at": "2026-09-16T00:00:00+00:00",
        "matches": [{"source": {"raw_sha256": _DIGEST}}],
    }
    report = build_source_research_report(
        snapshot,
        snapshot_sha256="1" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    row = next(item for item in report["sources"] if item["id"] == "openligadb_secondary_results")
    assert row["observed_http"]["evidence"] == ["snapshot.runtime.openligadb"]
    assert row["raw_hash_policy"]["observed_hash_count"] == 1


def test_open_meteo_geocoding_declares_place_fields_not_weather_fields():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="2" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    row = next(item for item in report["sources"] if item["id"] == "open_meteo_geocoding")
    assert row["field_coverage"]["declared"] == [
        "place_query",
        "geocoded_coordinates",
        "provider_place_id",
        "retrieved_at",
    ]
    assert "forecast_hour" not in row["field_coverage"]["declared"]
    assert "place resolution" in row["time_semantics"]


def test_blocked_source_probe_cannot_claim_network_access():
    probe = {
        "schema_version": "matchline.source_probe_evidence.v1",
        "probes": [
            {
                "source_id": "espn_schedule_summary",
                "attempted": True,
                "state": "success",
                "status": "ok",
                "network_opened": True,
                "status_codes": [200],
                "content_types": ["application/json"],
                "raw_hashes": [_DIGEST],
            }
        ],
    }
    with pytest.raises(ValueError, match="blocked source"):
        build_source_research_report(
            _snapshot(),
            snapshot_sha256="3" * 64,
            observed_at="2026-09-16T00:01:00+00:00",
            probe_evidence=probe,
        )


def test_unattempted_runtime_zero_is_reported_as_missing_not_observed_zero():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="4" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    row = next(item for item in report["sources"] if item["id"] == "espn_schedule_summary")
    assert row["observed_http"]["reported_record_count"] == 0
    assert row["observed_http"]["record_count"] is None


def test_inventory_and_probe_time_contract_is_immutable():
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="5" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    tampered = deepcopy(report)
    tampered["source_ids"][0] = "tampered"
    with pytest.raises(ValueError, match="source_ids"):
        validate_source_research_report(tampered)

    probe = {
        "schema_version": "matchline.source_probe_evidence.v1",
        "probes": [
            {
                "source_id": "wikidata_entities",
                "attempted": True,
                "state": "success",
                "status": "ok",
                "network_opened": True,
                "status_codes": [200],
                "content_types": ["application/json"],
                "raw_hashes": [_DIGEST],
                "request_started_at": "2026-09-16T00:02:00+00:00",
                "response_observed_at": "2026-09-16T00:02:01+00:00",
            }
        ],
    }
    with pytest.raises(ValueError, match="newer than report"):
        build_source_research_report(
            _snapshot(),
            snapshot_sha256="6" * 64,
            observed_at="2026-09-16T00:01:00+00:00",
            probe_evidence=probe,
        )


def test_hash_collection_exposes_truncation_without_dropping_count():
    snapshot = _snapshot()
    snapshot["openfootball_current"]["fixtures"] = [
        {"source": {"raw_sha256": f"{index:064x}"}} for index in range(12)
    ]
    report = build_source_research_report(
        snapshot,
        snapshot_sha256="7" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    row = next(item for item in report["sources"] if item["id"] == "openfootball_current")
    assert row["raw_hash_policy"]["observed_hash_count"] == 12
    assert row["raw_hash_policy"]["observed_hashes_truncated"] is True
    assert len(row["raw_hash_policy"]["observed_hashes"]) == 8
def test_validator_recomputes_referenced_snapshot_and_probe_file_hashes(tmp_path):
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text('{"as_of":"2026-09-16T00:00:00+00:00"}', encoding="utf-8")
    probe_path = tmp_path / "probe.json"
    probe_path.write_text('{"schema_version":"matchline.source_probe_evidence.v1","probes":[]}', encoding="utf-8")
    import hashlib

    snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    probe_sha = hashlib.sha256(probe_path.read_bytes()).hexdigest()
    report = build_source_research_report(
        {"as_of": "2026-09-16T00:00:00+00:00"},
        snapshot_sha256=snapshot_sha,
        snapshot_path=str(snapshot_path),
        probe_evidence={"schema_version": "matchline.source_probe_evidence.v1", "probes": []},
        probe_evidence_path=str(probe_path),
        probe_evidence_sha256=probe_sha,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    validate_source_research_report(report)
    snapshot_path.write_text('{"as_of":"2026-09-16T00:00:00+00:00","changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot file bytes"):
        validate_source_research_report(report)


def test_probe_success_requires_an_actual_opened_network_attempt():
    probe = {
        "schema_version": "matchline.source_probe_evidence.v1",
        "probes": [
            {
                "source_id": "wikidata_entities",
                "attempted": False,
                "state": "success",
                "status": "ok",
                "network_opened": False,
                "status_codes": [200],
                "content_types": ["application/json"],
                "raw_hashes": [_DIGEST],
            }
        ],
    }
    with pytest.raises(ValueError, match="real attempt"):
        build_source_research_report(
            _snapshot(),
            snapshot_sha256="8" * 64,
            observed_at="2026-09-16T00:01:00+00:00",
            probe_evidence=probe,
        )


def test_future_snapshot_and_field_contract_drift_are_rejected():
    with pytest.raises(ValueError, match="newer than report"):
        build_source_research_report(
            {"as_of": "2026-09-16T00:02:00+00:00"},
            snapshot_sha256="9" * 64,
            observed_at="2026-09-16T00:01:00+00:00",
        )
    report = build_source_research_report(
        _snapshot(),
        snapshot_sha256="a" * 64,
        observed_at="2026-09-16T00:01:00+00:00",
    )
    report["sources"][0]["field_coverage"]["declared"] = ["tampered_field"]
    with pytest.raises(ValueError, match="field declaration"):
        validate_source_research_report(report)
