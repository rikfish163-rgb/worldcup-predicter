from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any

import pytest

from league_platform import build_offline_bundle


class _MinimalBundleStore:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def snapshot(self) -> dict:
        return {
            "generated_at": "2026-08-25T00:00:00+00:00",
            "timezone": "Asia/Shanghai",
            "summary": {},
            "competitions": [],
            "matches": [],
            "current_data": {"source_registry": []},
            "strict_backtest": {"status": "stale"},
            "prospective_evaluation": {"status": "pending"},
            "news": {"provider": "RSS", "feeds": []},
        }

    def predictions(self) -> dict:
        return {"predictions": [], "blocked": []}


def _trusted_openfootball_fixture() -> dict[str, Any]:
    return {
        "id": "openfootball:premier-league:1001",
        "competition_id": "premier-league",
        "season": "2026-27",
        "kickoff_at": "2026-08-28T18:30:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "upcoming",
        "score": None,
        "source": {
            "name": "OpenFootball",
            "source_id": "openfootball:football.json:2026-27:en.1",
            "url": (
                "https://raw.githubusercontent.com/openfootball/football.json/"
                "master/2026-27/en.1.json"
            ),
            "retrieved_at": "2026-08-25T00:00:00+00:00",
            "raw_sha256": "a" * 64,
            "license": "CC0-1.0",
        },
    }


def _verified_openfootball_admission() -> dict[str, Any]:
    source_id = "openfootball:football.json:2026-27:en.1"
    return {
        "schema_version": "matchline.openfootball_raw_admission.v1",
        "status": "verified_current_raw",
        "policy_version": "v260",
        "observed_before": "2026-08-25T00:00:00+00:00",
        "source_ids": [source_id],
        "row_count": 1,
        "selected_records": [
            {
                "source_id": source_id,
                "retrieved_at": "2026-08-25T00:00:00+00:00",
                "record_sha256": "a" * 64,
                "raw_sha256": "b" * 64,
            }
        ],
        "admission_sha256": "c" * 64,
        "source_manifest_sha256": "d" * 64,
        "parser_contract_sha256": "e" * 64,
        "rows_sha256": "f" * 64,
    }


def test_compact_prediction_preserves_causal_training_cutoff() -> None:
    prediction = {
        "fixture_id": "openfootball:premier-league:1001",
        "as_of": "2026-08-25T12:00:00+00:00",
        "cutoff_at": "2026-08-25T11:00:00+00:00",
        "training_cutoff": "2026-08-24T18:00:00+00:00",
        "history_context": {"schema_version": "matchline.history_context.v1", "teams": {}},
    }

    compact = build_offline_bundle._compact_prediction(prediction)

    assert compact["training_cutoff"] == "2026-08-24T18:00:00+00:00"
    assert compact["history_context"]["schema_version"] == "matchline.history_context.v1"


def _stub_openfootball_replay(snapshot: dict, *, archive_root: Path) -> dict[str, Any]:
    admission = snapshot["fixture_feed"]["raw_archive_admission"]
    return {
        "status": admission["status"],
        "schema_version": admission["schema_version"],
        "observed_before": admission["observed_before"],
        "source_ids": admission["source_ids"],
        "admission_sha256": admission["admission_sha256"],
        "source_manifest_sha256": admission["source_manifest_sha256"],
        "parser_contract_sha256": admission["parser_contract_sha256"],
        "rows_sha256": admission["rows_sha256"],
        "raw_row_count": admission["row_count"],
        "published_fixture_count": len(snapshot["fixture_feed"]["fixtures"]),
    }


def test_met_norway_weather_is_projected_with_cc_by_lineage() -> None:
    fixture = _trusted_openfootball_fixture()
    snapshot = {
        "fixture_feed": {"provider": "OpenFootball", "fixtures": [fixture]},
        "met_norway_weather": {
            "provider": "MET Norway Locationforecast",
            "weather": [
                {
                    "fixture_id": fixture["id"],
                    "forecast_at": fixture["kickoff_at"],
                    "coordinate_confidence": "medium",
                    "coordinate_model_eligible": False,
                    "temperature_c": 18.0,
                    "humidity_percent": 70.0,
                    "precipitation_mm": 0.2,
                    "source": {
                        "name": "MET Norway Locationforecast",
                        "source_id": "met_norway_weather",
                        "url": "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51.5&lon=-0.28",
                        "retrieved_at": "2026-08-25T00:00:00+00:00",
                        "raw_sha256": "b" * 64,
                        "license": "CC-BY-4.0",
                    },
                }
            ],
        },
    }

    evidence = build_offline_bundle._compact_fixture_evidence(snapshot, [fixture])

    weather = evidence[fixture["id"]]["weather"]
    assert len(weather) == 1
    assert weather[0]["sourceId"] == "met_norway_weather"
    assert weather[0]["sourceLicense"] == "CC-BY-4.0"
    assert weather[0]["temperatureC"] == 18.0
    assert weather[0]["coordinateConfidence"] == "medium"
    assert weather[0]["coordinateModelEligible"] is False


def _bundle_snapshot(*, matches: list[dict] | None = None, **extra: Any) -> dict:
    return {
        "generated_at": "2026-08-25T00:00:00+00:00",
        "timezone": "Asia/Shanghai",
        "summary": {},
        "competitions": [],
        "matches": matches or [],
        "current_data": {"source_registry": []},
        "strict_backtest": {"status": "stale"},
        "prospective_evaluation": {"status": "pending"},
        "news": {"provider": "Public RSS", "feeds": []},
        **extra,
    }


def _assert_outputs_unchanged(outputs: dict[Path, bytes]) -> None:
    assert {path: path.read_bytes() for path in outputs} == outputs


def test_bundle_rejects_blocked_row_hidden_beside_trusted_fixture_before_any_output_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _trusted_openfootball_fixture()

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    raw_sources = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [fixture],
            "errors": [],
        },
        "espn_markets": {
            "provider": "ESPN event summary",
            "markets": [
                {
                    "fixture_id": fixture["id"],
                    "provider": "ESPN",
                    "american_odds": {"home": 110, "draw": 210, "away": 250},
                    "probability": {"home": 0.4, "draw": 0.3, "away": 0.3},
                    "retrieved_at": "2026-08-25T00:01:00+00:00",
                    "source": {
                        "name": "ESPN event summary",
                        "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/summary",
                        "raw_sha256": "b" * 64,
                        "rights_status": "operator_claimed_allow",
                        "commercial_reuse_verified": True,
                    },
                }
            ],
            "team_status": [],
            "fixture_updates": [],
            "incidents": [],
            "match_stats": [],
            "errors": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(raw_sources), encoding="utf-8")
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    outputs = {
        tmp_path / "offline_full.js": b"previous-safe-full",
        tmp_path / "public" / "offline_snapshot.json": b"previous-safe-compact",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_market_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=tmp_path / "offline_full.js",
            compact_output=tmp_path / "public" / "offline_snapshot.json",
        )

    _assert_outputs_unchanged(outputs)


def test_bundle_accepts_only_the_registered_openfootball_fixture_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _trusted_openfootball_fixture()

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [fixture],
                    "errors": [],
                    "raw_archive_admission": _verified_openfootball_admission(),
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    monkeypatch.setattr(
        build_offline_bundle,
        "verify_openfootball_snapshot_admission",
        _stub_openfootball_replay,
    )

    result = build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=live_path,
        strict_report_path=report_path,
        output=output,
        compact_output=compact_output,
        openfootball_raw_archive_dir=Path("/tmp/matchline-test-openfootball-raw"),
    )

    assert result["output"] == str(output)
    assert result["compact_output"] == str(compact_output)
    compact = json.loads(compact_output.read_text(encoding="utf-8"))
    assert compact["matches"][0]["id"] == fixture["id"]
    lineage = compact["fixture_evidence"][fixture["id"]]["intelligence"][0]
    assert lineage["sourceId"] == fixture["source"]["source_id"]


def test_bundle_attaches_snapshot_fixture_provenance_to_verified_prediction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _trusted_openfootball_fixture()
    prediction = {
        "fixture_id": fixture["id"],
        "competition_id": fixture["competition_id"],
        "season": fixture["season"],
        "kickoff_at": fixture["kickoff_at"],
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "source_name": "OpenFootball",
        "source_license_status": "CC0-1.0",
        "source_sha256": fixture["source"]["raw_sha256"],
        "status": "research_only",
        "quality_gate": "blocked_for_production",
        "primary_probability_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3},
    }

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def public_predictions(self, *, include_research_drafts: bool = False) -> dict:
            result: dict[str, Any] = {
                "predictions": [prediction],
                "blocked": [],
            }
            if include_research_drafts:
                result["research_predictions"] = {
                    "status": "unavailable",
                    "predictions": [],
                    "blocked": [],
                }
            return result

    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [fixture],
                    "errors": [],
                    "raw_archive_admission": _verified_openfootball_admission(),
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    monkeypatch.setattr(
        build_offline_bundle,
        "verify_openfootball_snapshot_admission",
        _stub_openfootball_replay,
    )

    output = tmp_path / "offline_full.js"
    compact_output = tmp_path / "offline_snapshot.json"
    build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=live_path,
        strict_report_path=report_path,
        output=output,
        compact_output=compact_output,
        openfootball_raw_archive_dir=Path("/tmp/matchline-test-openfootball-raw"),
    )

    compact = json.loads(compact_output.read_text(encoding="utf-8"))
    assert [item["fixture_id"] for item in compact["predictions"]] == [fixture["id"]]


def test_prediction_source_contract_must_match_snapshot_fixture() -> None:
    fixture = _trusted_openfootball_fixture()
    forged_source = {
        **fixture["source"],
        "raw_sha256": "0" * 64,
    }
    row = {
        "fixture_id": fixture["id"],
        "competition_id": fixture["competition_id"],
        "season": fixture["season"],
        "kickoff_at": fixture["kickoff_at"],
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "source": forged_source,
        "status": "research_only",
    }
    bound = build_offline_bundle._bind_snapshot_prediction_provenance(
        {"predictions": [row]},
        snapshot=_bundle_snapshot(matches=[fixture]),
    )
    with pytest.raises(
        ValueError,
        match=r"offline_bundle_source_provenance_missing:predictions\.predictions\[0\]",
    ):
        build_offline_bundle._require_prediction_rows(
            bound,
            path="predictions",
            allow_verified_openfootball=True,
        )


def test_unbound_prediction_is_quarantined_without_blocking_other_rows() -> None:
    fixture = _trusted_openfootball_fixture()
    result = build_offline_bundle._quarantine_unbound_predictions(
        {
            "predictions": [
                {"fixture_id": fixture["id"], "status": "research_only"},
                {
                    "fixture_id": "openfootball:premier-league:valid",
                    "status": "research_only",
                    "source": {
                        "name": "OpenFootball",
                        "source_id": fixture["source"]["source_id"],
                        "url": fixture["source"]["url"],
                        "raw_sha256": fixture["source"]["raw_sha256"],
                        "license": "CC0-1.0",
                    },
                },
            ],
            "blocked": [],
        }
    )

    assert len(result["predictions"]) == 1
    assert result["predictions"][0]["fixture_id"].endswith(":valid")
    assert result["blocked"] == [
        {
            "reason": "prediction_source_contract_unverified",
            "reason_code": "source_snapshot_mismatch",
            "status": "blocked",
            "message": "预测来源未与本轮已验证赛程原始归档精确绑定，已隔离。",
        }
    ]


def test_formal_prediction_cannot_use_research_provider_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"offline_bundle_source_provenance_missing:predictions\.predictions\[0\]",
    ):
        build_offline_bundle._require_prediction_rows(
            {
                "predictions": [
                    {
                        "fixture_id": "openfootball:premier-league:1001",
                        "status": "research_only",
                        "providers": ["OpenFootball"],
                    }
                ]
            },
            path="predictions",
            allow_verified_openfootball=True,
        )


def test_live_overlay_cannot_self_authorize_blocked_provider_for_redistribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    overlay_path = tmp_path / "live_overlay.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "matchline.live_overlay.v1",
                "status": "ok",
                "fixture_count": 1,
                "fixtures": [
                    {
                        "fixture_id": "espn:1",
                        "status": "live",
                        "score": {"home": 1, "away": 0},
                        "source": {
                            "name": "ESPN event summary",
                            "source_id": "openfootball_current",
                            "rights_status": "verified_cc0",
                            "commercial_reuse_verified": True,
                        },
                        "enters_model": False,
                    }
                ],
                "source": {
                    "name": "ESPN event summary",
                    "source_id": "openfootball_current",
                },
                "policy": {
                    "network_opened": False,
                    "redistribution_allowed": True,
                    "model_eligible": False,
                },
            }
        ),
        encoding="utf-8",
    )
    outputs = {
        tmp_path / "offline_full.js": b"previous-safe-full",
        tmp_path / "public" / "offline_snapshot.json": b"previous-safe-compact",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_market_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            live_overlay_path=overlay_path,
            strict_report_path=report_path,
            output=tmp_path / "offline_full.js",
            compact_output=tmp_path / "public" / "offline_snapshot.json",
        )

    _assert_outputs_unchanged(outputs)


def test_existing_malformed_live_overlay_fails_before_any_output_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    overlay_path = tmp_path / "live_overlay.json"
    overlay_path.write_text('{"schema_version":', encoding="utf-8")
    outputs = {
        tmp_path / "offline_full.js": b"previous-safe-full",
        tmp_path / "public" / "offline_snapshot.json": b"previous-safe-compact",
        tmp_path / "public" / "offline_snapshot.js": b"previous-safe-compact-js",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    with pytest.raises(ValueError, match="live_overlay_invalid_json"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            live_overlay_path=overlay_path,
            strict_report_path=report_path,
            output=tmp_path / "offline_full.js",
            compact_output=tmp_path / "public" / "offline_snapshot.json",
            compact_js_output=tmp_path / "public" / "offline_snapshot.js",
        )

    _assert_outputs_unchanged(outputs)


def test_multi_output_replace_failure_rolls_back_every_previous_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    outputs = {
        tmp_path / "offline_full.js": b"previous-safe-full",
        tmp_path / "public" / "offline_snapshot.json": b"previous-safe-compact",
        tmp_path / "public" / "offline_snapshot.js": b"previous-safe-compact-js",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    real_replace = Path.replace
    replace_calls = 0

    def fail_second_replace(source: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected second output replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected second output replace failure"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=tmp_path / "offline_full.js",
            compact_output=tmp_path / "public" / "offline_snapshot.json",
            compact_js_output=tmp_path / "public" / "offline_snapshot.js",
        )

    _assert_outputs_unchanged(outputs)


@pytest.mark.parametrize(
    ("contamination", "expected_reason"),
    [
        (
            "field_source_cfl",
            "source_rights_blocked:cfl_official_current:redistribution",
        ),
        (
            "schedule_overlay_espn",
            "source_rights_blocked:espn_schedule_summary:redistribution",
        ),
        (
            "venue_unknown",
            "source_rights_unknown_provider:mystery_venue_source",
        ),
        (
            "provider_fixture_id_official",
            "source_rights_blocked:official_premier_league_lineups:redistribution",
        ),
        (
            "unscoped_odds",
            r"offline_bundle_source_provenance_missing:snapshot\.matches\[0\]\.odds",
        ),
    ],
)
def test_trusted_openfootball_fixture_cannot_launder_field_level_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contamination: str,
    expected_reason: str,
) -> None:
    fixture = _trusted_openfootball_fixture()
    if contamination == "field_source_cfl":
        fixture["field_sources"] = {
            "kickoff_at": {
                "name": "CFL official",
                "source_id": "openfootball_current",
            }
        }
    elif contamination == "schedule_overlay_espn":
        fixture["schedule_overlay"] = {
            "provider": "ESPN",
            "provider_fixture_id": "espn-1",
            "fields": ["kickoff_at"],
        }
    elif contamination == "venue_unknown":
        fixture["venue"] = {
            "name": "Venue",
            "source": {
                "name": "Unregistered venue operator",
                "source_id": "mystery_venue_source",
            },
        }
    elif contamination == "unscoped_odds":
        fixture["odds"] = {"home": 2.0, "draw": 3.0, "away": 4.0}
    else:
        fixture["provider_fixture_ids"] = {
            "OpenFootball": fixture["id"],
            "premier_league_official": "official-1",
        }

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(ValueError, match=expected_reason):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_fixture_feed_raw_archive_admission_is_bounded_metadata_not_unknown_fact_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _trusted_openfootball_fixture()

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    raw_sources = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "retrieved_at": "2026-08-25T00:00:00+00:00",
            "status": "ok",
            "fixtures": [fixture],
            "errors": [],
            "raw_archive_admission": _verified_openfootball_admission(),
        }
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(raw_sources), encoding="utf-8")
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    monkeypatch.setattr(
        build_offline_bundle,
        "verify_openfootball_snapshot_admission",
        _stub_openfootball_replay,
    )

    result = build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=live_path,
        strict_report_path=report_path,
        output=output,
        openfootball_raw_archive_dir=Path("/tmp/matchline-test-openfootball-raw"),
    )

    assert result["output"] == str(output)
    assert output.exists()


def test_openligadb_current_display_rights_do_not_authorize_static_redistribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = {
        **_trusted_openfootball_fixture(),
        "id": "openligadb:1001",
        "source": {
            "name": "OpenLigaDB",
            "source_id": "openligadb_secondary_results",
            "url": "https://api.openligadb.de/getmatchdata/bl1/2026",
            "license": "ODbL-1.0",
            "license_url": "https://www.openligadb.de/lizenz",
            "attribution_required": True,
        },
    }

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[fixture])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:openligadb_secondary_results:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_live_overlay_cannot_claim_exact_openfootball_fixture_identity_as_its_producer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    fixture = _trusted_openfootball_fixture()
    fixture.update(
        {
            "fixture_id": fixture["id"],
            "status": "live",
            "score": {"home": 1, "away": 0},
        }
    )
    overlay_path = tmp_path / "live_overlay.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "matchline.live_overlay.v1",
                "status": "ok",
                "fixture_count": 1,
                "fixtures": [fixture],
                "source": fixture["source"],
                "policy": {"network_opened": False},
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match="offline_bundle_live_overlay_producer_unverified:openfootball_current",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            live_overlay_path=overlay_path,
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_empty_live_overlay_cannot_hide_facts_behind_openfootball_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    overlay_path = tmp_path / "live_overlay.json"
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "matchline.live_overlay.v1",
                "status": "no_live_fixtures",
                "fixture_count": 0,
                "fixtures": [],
                "latest_score": {
                    "fixture_id": "openfootball:premier-league:1001",
                    "score": {"home": 1, "away": 0},
                },
                "source": _trusted_openfootball_fixture()["source"],
                "policy": {"network_opened": False},
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match=(
            r"offline_bundle_diagnostic_contains_factual_row:"
            r"snapshot\.live_overlay\.latest_score\.fixture_id"
        ),
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            live_overlay_path=overlay_path,
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_bundle_rejects_historical_fact_without_redistribution_rights_when_live_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    historical_match = {
        "id": "football-data:premier-league:2025:1",
        "competition_id": "premier-league",
        "season": "2025-26",
        "kickoff_at": "2026-05-01T14:00:00+00:00",
        "home_team": "Home",
        "away_team": "Away",
        "status": "finished",
        "score": {"home": 1, "away": 0},
        "source": {
            "name": "football-data.co.uk",
            "source_id": "football_data_historical",
            "url": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
        },
    }

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[historical_match])

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:football_data_historical:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_bundle_rejects_unknown_source_backed_section_before_any_output_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(
                operator_feed={
                    "provider": "Operator-declared feed",
                    "rows": [
                        {
                            "fixture_id": "operator:1",
                            "status": "upcoming",
                            "source": {
                                "name": "Operator-declared feed",
                                "source_id": "operator_claimed_new_source",
                            },
                        }
                    ],
                }
            )

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match="source_rights_unknown_provider:operator_claimed_new_source",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_unknown_provider_cannot_borrow_openfootball_historical_rights(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore(_MinimalBundleStore):
        def snapshot(self) -> dict:
            return _bundle_snapshot(
                cloned_history={
                    "provider": "Attacker clone",
                    "rows": [
                        {
                            "fixture_id": "clone:1",
                            "home_team": "Home",
                            "away_team": "Away",
                            "source": {
                                "name": "Attacker clone",
                                "source_id": "openfootball_historical",
                                "license": "CC0-1.0",
                            },
                        }
                    ],
                }
            )

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(ValueError, match="openfootball_source_contract_unverified"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_unclassified_unknown_mapping_fails_closed_before_output_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore(_MinimalBundleStore):
        def snapshot(self) -> dict:
            return _bundle_snapshot(
                unclassified_payload={
                    "opaque_rows": {"row_1": {"club_a": "Home", "club_b": "Away"}}
                }
            )

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match="offline_bundle_unknown_source_section:snapshot.unclassified_payload",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


@pytest.mark.parametrize(
    ("section_key", "rows_key", "expected_source_id"),
    [
        ("cfl_official", "fixtures", "cfl_official_current"),
        ("espn_markets", "fixture_updates", "espn_market_summary"),
        ("espn_rosters", "rosters", "espn_team_rosters"),
        ("espn_injuries", "reports", "espn_injury_reports"),
        ("sports_lottery", "matches", "sports_lottery_official"),
        ("news", "feeds", "public_rss_news"),
        ("weather", "weather", "open_meteo_weather"),
        ("geocoding", "geocodes", "open_meteo_geocoding"),
        ("understat", "team_features", "understat_xg"),
        ("sofascore", "events", "sofascore_prematch"),
        ("fotmob", "lineups", "fotmob_public_api"),
        ("oddstorm", "lines", "oddstorm_market_comparison"),
        ("openligadb", "matches", "openligadb_secondary_results"),
        (
            "premier_league_official",
            "lineups",
            "official_premier_league_lineups",
        ),
        ("laliga_official", "lineups", "official_laliga_lineups"),
        ("bundesliga_official", "lineups", "official_bundesliga_lineups"),
        ("serie_a_official", "lineups", "official_serie_a_lineups"),
        ("ligue1_official", "lineups", "official_ligue1_lineups"),
    ],
)
def test_each_known_blocked_source_section_is_preflighted_before_bundle_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    section_key: str,
    rows_key: str,
    expected_source_id: str,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                section_key: {
                    "provider": section_key,
                    rows_key: [
                        {
                            "fixture_id": "provider:1",
                            "source": {
                                "name": "OpenFootball",
                                "source_id": "openfootball_current",
                                "license": "CC0-1.0",
                            },
                        }
                    ],
                    "errors": [],
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match=f"source_rights_blocked:{expected_source_id}:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_fixture_feed_secondary_projection_cannot_hide_blocked_provider_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _trusted_openfootball_fixture()
    blocked = {
        **fixture,
        "id": "espn:secondary",
        "source": {
            "name": "ESPN",
            "source_id": "openfootball_current",
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "fixtures": [fixture],
                    "upcoming_7_days": [blocked],
                    "errors": [],
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_schedule_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_empty_blocked_source_health_can_remain_when_network_was_not_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    live_path = tmp_path / "current.json"
    live_path.write_text(
        json.dumps(
            {
                "espn_markets": {
                    "provider": "ESPN event summary",
                    "status": "rights_blocked",
                    "markets": [],
                    "team_status": [],
                    "fixture_updates": [],
                    "incidents": [],
                    "match_stats": [],
                    "errors": [],
                    "network_opened": False,
                }
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"

    result = build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=live_path,
        strict_report_path=report_path,
        output=output,
    )

    assert result["output"] == str(output)
    assert output.exists()


def test_bundle_rejects_provider_health_without_network_false_before_output_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore(_MinimalBundleStore):
        def snapshot(self) -> dict:
            return _bundle_snapshot(
                current_data={
                    "source_registry": [],
                    "provider_errors": {"espn": ["timeout after network request"]},
                }
            )

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match=(
            "offline_bundle_blocked_diagnostic_requires_network_opened_false:"
            r"snapshot\.current_data\.provider_errors\.espn"
        ),
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


@pytest.mark.parametrize(
    "rights_metadata",
    [
        {},
        {
            "rights_status": "blocked_pending_express_written_permission",
            "access_allowed": False,
        },
    ],
)
def test_bundle_refuses_unlicensed_legacy_espn_live_pointer_without_overwriting_public_artifacts(
    monkeypatch,
    tmp_path: Path,
    rights_metadata: dict,
) -> None:
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-25T00:00:00+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {"source_registry": []},
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def public_predictions(self, **_kwargs) -> dict:
            return {
                "predictions": [],
                "blocked": [],
                "research_predictions": {
                    "status": "unavailable",
                    "predictions": [],
                    "blocked": [],
                },
            }

    live_path = tmp_path / "current.json"
    live_payload = {
        "as_of": "2026-08-25T00:00:00+00:00",
        "espn": {
            "provider": "ESPN",
            "fixtures": [{"id": "espn:cached"}],
            **rights_metadata,
        },
    }
    live_path.write_text(json.dumps(live_payload), encoding="utf-8")
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    output.write_text("previous-safe-full", encoding="utf-8")
    compact_output.parent.mkdir(parents=True)
    compact_output.write_text("previous-safe-compact", encoding="utf-8")
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)

    with pytest.raises(ValueError, match="legacy_espn_serving_rights_missing"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=output,
            compact_output=compact_output,
        )

    assert output.read_text(encoding="utf-8") == "previous-safe-full"
    assert compact_output.read_text(encoding="utf-8") == "previous-safe-compact"
    assert json.loads(live_path.read_text(encoding="utf-8")) == live_payload


@pytest.mark.parametrize(
    ("live_contents", "expected_error"),
    [
        ("{not-json", "live_snapshot_invalid_json"),
        ("[]", "live_snapshot_root_not_object"),
    ],
)
def test_bundle_refuses_invalid_existing_live_snapshot_before_replacing_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_contents: str,
    expected_error: str,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    live_path = tmp_path / "current.json"
    live_path.write_text(live_contents, encoding="utf-8")
    outputs = {
        tmp_path / "public" / "offline_full.js": b"previous-safe-full",
        tmp_path / "public" / "offline_snapshot.json": b"previous-safe-compact-json",
        tmp_path / "public" / "offline_snapshot.js": b"previous-safe-compact-js",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    with pytest.raises(ValueError, match=expected_error):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=tmp_path / "public" / "offline_full.js",
            compact_output=tmp_path / "public" / "offline_snapshot.json",
            compact_js_output=tmp_path / "public" / "offline_snapshot.js",
        )

    assert {path: path.read_bytes() for path in outputs} == outputs


def test_bundle_prepares_every_requested_output_before_replacing_any_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)

    def non_serializable_compact_snapshot(*_args: object, **_kwargs: object) -> dict:
        return {"not_serializable": object()}

    monkeypatch.setattr(
        build_offline_bundle,
        "_compact_site_snapshot",
        non_serializable_compact_snapshot,
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    publish_dir = tmp_path / "public"
    outputs = {
        publish_dir / "offline_full.js": b"previous-safe-full",
        publish_dir / "offline_snapshot.json": b"previous-safe-compact-json",
        publish_dir / "offline_snapshot.js": b"previous-safe-compact-js",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    with pytest.raises(TypeError, match="not JSON serializable"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=publish_dir / "offline_full.js",
            compact_output=publish_dir / "offline_snapshot.json",
            compact_js_output=publish_dir / "offline_snapshot.js",
        )

    assert {path: path.read_bytes() for path in outputs} == outputs
    assert {path.name for path in publish_dir.iterdir()} == {
        "offline_full.js",
        "offline_snapshot.json",
        "offline_snapshot.js",
    }


def test_bundle_cleans_staged_files_when_preparation_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", _MinimalBundleStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    publish_dir = tmp_path / "public"
    outputs = {
        publish_dir / "offline_full.js": b"previous-safe-full",
        publish_dir / "offline_snapshot.json": b"previous-safe-compact-json",
        publish_dir / "offline_snapshot.js": b"previous-safe-compact-js",
    }
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    real_fdopen = build_offline_bundle.os.fdopen
    open_count = 0

    def fail_second_staged_write(
        descriptor: int,
        mode: str,
        *,
        encoding: str,
    ) -> IO[Any]:
        nonlocal open_count
        open_count += 1
        if open_count == 2:
            raise OSError("simulated staged write failure")
        return real_fdopen(descriptor, mode, encoding=encoding)

    monkeypatch.setattr(build_offline_bundle.os, "fdopen", fail_second_staged_write)

    with pytest.raises(OSError, match="simulated staged write failure"):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=None,
            strict_report_path=report_path,
            output=publish_dir / "offline_full.js",
            compact_output=publish_dir / "offline_snapshot.json",
            compact_js_output=publish_dir / "offline_snapshot.js",
        )

    assert {path: path.read_bytes() for path in outputs} == outputs
    assert {path.name for path in publish_dir.iterdir()} == {
        "offline_full.js",
        "offline_snapshot.json",
        "offline_snapshot.js",
    }


def test_compact_match_preserves_provider_identity_and_source_backed_venue():
    fixture_id = "openfootball:bundesliga:fixture-1"
    snapshot = {
        "matches": [
            {
                "id": fixture_id,
                "competition_id": "bundesliga",
                "season": "2026",
                "kickoff_at": "2026-08-28T18:30:00+00:00",
                "home_team": "Home",
                "away_team": "Away",
                "status": "upcoming",
            }
        ],
    }
    raw_sources = {
        "fixture_feed": {
            "fixtures": [
                {
                    **snapshot["matches"][0],
                    "provider_fixture_ids": {"OpenFootball": fixture_id},
                    "venue": {"name": "Example Arena", "city": "Example City"},
                    "source": {"name": "OpenFootball"},
                }
            ]
        }
    }

    rows = build_offline_bundle._compact_matches(
        snapshot,
        {"predictions": []},
        raw_sources=raw_sources,
    )

    assert rows[0]["provider_fixture_ids"] == {"OpenFootball": fixture_id}
    assert rows[0]["venue"] == {"name": "Example Arena", "city": "Example City"}


def test_runtime_archive_projection_is_bounded_and_does_not_claim_verification(tmp_path: Path):
    manifest = tmp_path / "runtime-archive-manifest.json"
    (tmp_path / "runtime-snapshot-demo.tar.zst").write_bytes(b"archive")
    manifest.write_text(
        json.dumps(
            {
                "status": "archived",
                "finished_at": "2026-08-22T07:24:20Z",
                "archive_file": "runtime-snapshot-demo.tar.zst",
                "archive_sha256": "a" * 64,
                "archive_bytes": 7,
                "free_bytes_after": 100,
                "retained_archive_count": 1,
                "runtime_persistence": "durable_packed_archive",
                "restore_boundary": "read_only_recovery_artifact",
                "source": {"file_count": 2, "bytes": 10, "state_sha256": "b" * 64},
            }
        ),
        encoding="utf-8",
    )

    projected = build_offline_bundle._load_runtime_archive_projection(manifest)

    assert projected == {
        "status": "archived",
        "manifest_id": manifest.name,
        "captured_at": "2026-08-22T07:24:20Z",
        "archive_file": "runtime-snapshot-demo.tar.zst",
        "archive_exists": True,
        "archive_sha256": "a" * 64,
        "archive_bytes": 7,
        "source_file_count": 2,
        "source_bytes": 10,
        "source_state_sha256": "b" * 64,
        "free_bytes_after": 100,
        "retained_archive_count": 1,
        "runtime_persistence": "durable_packed_archive",
        "restore_boundary": "read_only_recovery_artifact",
        "verification": "not_checked",
        "verified_at": None,
    }


def test_runtime_archive_projection_marks_missing_manifest_without_inventing_health(
    tmp_path: Path,
):
    projected = build_offline_bundle._load_runtime_archive_projection(tmp_path / "missing.json")

    assert projected == {
        "status": "manifest_unavailable",
        "manifest_id": "missing.json",
        "archive_exists": False,
        "verification": "not_checked",
    }


def test_runtime_archive_projection_accepts_only_exactly_bound_verification_receipt(
    tmp_path: Path,
):
    manifest = tmp_path / "runtime-archive-manifest.json"
    archive = tmp_path / "runtime-snapshot-demo.tar.zst"
    archive.write_bytes(b"archive")
    manifest.write_text(
        json.dumps(
            {
                "status": "archived",
                "finished_at": "2026-08-23T06:10:00Z",
                "archive_file": archive.name,
                "archive_sha256": "a" * 64,
                "archive_bytes": 7,
                "source": {"file_count": 2, "bytes": 10, "state_sha256": "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "runtime-archive-verification.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "matchline.runtime_archive_verification.v1",
                "status": "verified",
                "verified_at": "2026-08-23T06:11:00Z",
                "manifest_file": manifest.name,
                "archive_file": archive.name,
                "archive_sha256": "a" * 64,
                "source_state_sha256": "b" * 64,
                "archive_member_files": 2,
            }
        ),
        encoding="utf-8",
    )

    projected = build_offline_bundle._load_runtime_archive_projection(manifest)

    assert projected["verification"] == "verified"
    assert projected["verified_at"] == "2026-08-23T06:11:00Z"
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload["archive_sha256"] = "c" * 64
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    stale = build_offline_bundle._load_runtime_archive_projection(manifest)
    assert stale["verification"] == "stale_receipt"
    assert stale["verified_at"] is None


def test_strict_compatibility_projects_only_ordered_market_confidence_intervals():
    value = {
        "model": "locked-v1",
        "sample_n": 20,
        "held_out_seasons": ["2526"],
        "warmup_seasons": ["2324"],
        "data_cutoff": "2026-05-24T00:00:00+00:00",
        "targets": {},
        "gates": {"three_way": {"status": "research_only"}},
        "three_way": {
            "model_on_market_sample": {
                "sample_n": 10,
                "brier": 0.58,
                "log_loss": 0.97,
                "rps": 0.2,
                "ece": 0.03,
            },
            "model": {"sample_n": 20, "brier": 0.59, "log_loss": 0.98, "rps": 0.21, "ece": 0.04},
            "historical_frequency_on_market_sample": {
                "sample_n": 10,
                "brier": 0.6,
                "log_loss": 1.0,
                "rps": 0.22,
                "ece": 0.05,
            },
            "historical_frequency": {
                "sample_n": 20,
                "brier": 0.61,
                "log_loss": 1.01,
                "rps": 0.23,
                "ece": 0.06,
            },
            "market": {
                "sample_n": 10,
                "brier": 0.581,
                "log_loss": 0.971,
                "rps": 0.201,
                "ece": 0.031,
            },
            "model_minus_market_brier_ci": {
                "mean": -0.001,
                "lower": -0.01,
                "upper": 0.008,
                "level": 0.95,
            },
            "model_brier_ci": {"mean": 0.58, "lower": 0.5, "upper": 0.7, "level": 0.95},
            "market_brier_ci": {"mean": 0.581, "lower": 0.51, "upper": 0.71, "level": 0.95},
            "independent_scoreline": {
                "status": "evaluated",
                "sample_n": 20,
                "market_sample_n": 10,
                "model": {
                    "sample_n": 20,
                    "brier": 0.57,
                    "log_loss": 0.96,
                    "rps": 0.19,
                    "ece": 0.01,
                },
                "market": {
                    "sample_n": 10,
                    "brier": 0.56,
                    "log_loss": 0.95,
                    "rps": 0.18,
                    "ece": 0.02,
                },
                "model_on_market_sample": {
                    "sample_n": 10,
                    "brier": 0.58,
                    "log_loss": 0.98,
                    "rps": 0.20,
                    "ece": 0.01,
                },
                "model_minus_market_brier_ci": {
                    "mean": 0.02,
                    "lower": 0.01,
                    "upper": 0.03,
                    "level": 0.95,
                },
            },
        },
    }
    projected = build_offline_bundle._strict_compatibility(value)
    assert projected["market_comparison"]["brier_delta_ci"] == {
        "mean": -0.001,
        "lower": -0.01,
        "upper": 0.008,
        "level": 0.95,
    }
    value["three_way"]["model_minus_market_brier_ci"] = {
        "mean": 0.2,
        "lower": 0.3,
        "upper": 0.1,
        "level": 0.95,
    }
    assert (
        build_offline_bundle._strict_compatibility(value)["market_comparison"]["brier_delta_ci"]
        is None
    )
    independent = projected["independent_scoreline"]
    assert independent["status"] == "evaluated"
    assert independent["sample_n"] == 20
    assert independent["market_sample_n"] == 10
    assert independent["model_on_market_sample"]["brier_score"] == 0.58
    assert independent["market_comparison"]["brier_delta_ci"] == {
        "mean": 0.02,
        "lower": 0.01,
        "upper": 0.03,
        "level": 0.95,
    }


def test_bundle_preserves_stale_strict_report_marker(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-17T05:48:19+00:00",
                "competitions": [{"id": "premier-league", "model_health": {}}],
                "strict_backtest": {
                    "status": "stale",
                    "report_generated_at": "2026-08-17T05:02:11+00:00",
                    "audit": {
                        "reason": "strict_report_prospective_lock_mismatch",
                    },
                },
            }

        def predictions(self) -> dict:
            return {"predictions": []}

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps(
            {
                "leagues": {"premier-league": {}},
                "combined": {"sample_n": 123},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "offline_data.js"
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)

    result = build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=tmp_path / "live.json",
        strict_report_path=report_path,
        output=output,
    )

    assert result["strict_report_status"] == "stale"
    assert result["strict_sample_n"] == 0
    text = output.read_text(encoding="utf-8")
    snapshot_text = text.split("\nwindow.__MATCHLINE_OFFLINE_PREDICTIONS__ = ", 1)[0]
    snapshot = json.loads(snapshot_text.split(" = ", 1)[1].rstrip(";\n"))
    assert snapshot["build_id"].startswith("bundle-")
    assert snapshot["strict_backtest"]["status"] == "stale"
    assert "strict_model_health" not in snapshot["competitions"][0]


def test_bundle_rejects_compact_js_fixture_evidence_from_blocked_source(
    monkeypatch,
    tmp_path: Path,
):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-21T05:48:19+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [],
                "matches": [
                    {
                        "id": "espn:1",
                        "status": "finished",
                        "kickoff_at": "2026-08-20T12:00:00+00:00",
                        "home_team": "A",
                        "away_team": "B",
                        "score": {"home": 1, "away": 0},
                        "current_features": {},
                    }
                ],
                "current_data": {"as_of": "2026-08-21T05:48:19+00:00", "source_registry": []},
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self):
            return {"predictions": [], "blocked": []}

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    live_path = tmp_path / "live.json"
    live_path.write_text(
        json.dumps(
            {
                "espn": {
                    "provider": "ESPN",
                    "rights_status": "operator_authorization_reference_supplied",
                    "access_allowed": True,
                    "authorization_reference": "test-contract:event-projection",
                    "commercial_reuse_verified_by_code": True,
                    "fixtures": [],
                    "network_opened": False,
                },
                "espn_markets": {
                    "incidents": [
                        {
                            "fixture_id": "espn:1",
                            "retrieved_at": "2026-08-21T05:49:00+00:00",
                            "events": [{"type": "Goal", "text": "A scores", "minute": "12'"}],
                            "source": {
                                "name": "ESPN",
                                "url": "https://example.test/summary",
                                "raw_sha256": "a" * 64,
                            },
                        }
                    ],
                    "match_stats": [],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    output = tmp_path / "full.js"
    compact_js = tmp_path / "public" / "offline.js"
    output.write_bytes(b"previous-safe-full")
    compact_js.parent.mkdir(parents=True, exist_ok=True)
    compact_js.write_bytes(b"previous-safe-compact-js")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_market_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=live_path,
            strict_report_path=report_path,
            output=output,
            compact_js_output=compact_js,
        )

    assert output.read_bytes() == b"previous-safe-full"
    assert compact_js.read_bytes() == b"previous-safe-compact-js"


def test_compact_current_data_rejects_blocked_match_center_rows(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-21T05:48:19+00:00",
                "timezone": "UTC",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {
                    "as_of": "2026-08-21T05:48:19+00:00",
                    "source_registry": [],
                    "match_center": {
                        "provider": "ESPN event summary",
                        "live": [{"fixture_id": "espn:1", "status": "live"}],
                        "recent_finished": [],
                    },
                },
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self):
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    output = tmp_path / "offline_data.js"
    output.write_bytes(b"previous-safe-full")
    compact_output.parent.mkdir(parents=True, exist_ok=True)
    compact_output.write_bytes(b"previous-safe-compact")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_market_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
            compact_output=compact_output,
        )

    assert output.read_bytes() == b"previous-safe-full"
    assert compact_output.read_bytes() == b"previous-safe-compact"


def test_bundle_projects_bounded_intelligence_ledger_summary(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-20T05:48:19+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {"as_of": "2026-08-20T05:48:19+00:00", "source_registry": []},
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "prospective-lock-integrity-test-latest.json").write_text(
        json.dumps(
            {
                "checked_at": "2026-08-20T05:49:00+00:00",
                "intelligence_ledger": {
                    "status": "quarantined_read_errors",
                    "physical_records": 101,
                    "valid_json_records": 99,
                    "recovered_json_records": 1,
                    "read_error_count": 2,
                    "quarantined_read_error_count": 2,
                    "unquarantined_read_error_count": 0,
                    "quarantine_entry_count": 2,
                    "sha256": "a" * 64,
                    "quarantine_sha256": "b" * 64,
                    "read_errors": [{"line": 4}, {"line": 8}],
                },
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)

    build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=tmp_path / "live.json",
        strict_report_path=report_path,
        output=tmp_path / "offline_data.js",
        compact_output=compact_output,
        evidence_dir=evidence_dir,
    )

    compact = json.loads(compact_output.read_text(encoding="utf-8"))
    assert compact["current_data"]["intelligence_ledger"] == {
        "status": "quarantined_read_errors",
        "physical_records": 101,
        "valid_json_records": 99,
        "recovered_json_records": 1,
        "read_error_count": 2,
        "quarantined_read_error_count": 2,
        "unquarantined_read_error_count": 0,
        "quarantine_entry_count": 2,
        "sha256": "a" * 64,
        "quarantine_sha256": "b" * 64,
        "checked_at": "2026-08-20T05:49:00+00:00",
        "evidence_pointer": "prospective-lock-integrity-test-latest.json:intelligence_ledger",
    }


def test_bundle_refuses_sites_snapshot_rows_without_source_provenance(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-17T05:48:19+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {"finished_matches": 2},
                "competitions": [],
                "matches": [
                    {
                        "id": "espn:1",
                        "competition_id": "csl",
                        "season": "2026",
                        "kickoff_at": "2026-08-18T11:35:00+00:00",
                        "home_team": "Home",
                        "away_team": "Away",
                        "status": "upcoming",
                        "score": None,
                        "market_probability": None,
                    },
                    {
                        "id": "historical:1",
                        "competition_id": "csl",
                        "season": "2025",
                        "kickoff_at": "2026-08-16T11:35:00+00:00",
                        "home_team": "Home",
                        "away_team": "Away",
                        "status": "finished",
                        "score": {"home": 1, "away": 0},
                        "market_probability": None,
                    },
                ],
                "current_data": {
                    "as_of": "2026-08-17T05:48:19+00:00",
                    "source_registry": [
                        {
                            "id": "ESPN",
                            "name": "ESPN",
                            "source_tier": "public_provider",
                            "access_policy": "public_https",
                            "model_policy": "eligible",
                            "status": "enabled",
                            "runtime": {
                                "status": "fresh",
                                "checked_at": "2026-08-17T05:48:19+00:00",
                                "rights_status": "public_reuse_confirmed",
                                "terms_url": "https://example.com/terms",
                                "network_opened": True,
                                "access_allowed": True,
                                "authorization_required": False,
                                "record_count": 1,
                                "errors": ["hidden"],
                                "match_discovery": {
                                    "status": "fresh",
                                    "page_count": 4,
                                    "records_seen": 380,
                                    "matched_count": 2,
                                    "truncated": False,
                                },
                                "fixture_join": {
                                    "status": "exact",
                                    "joined_count": 10,
                                    "unmatched_count": 0,
                                    "model_admission": "display_only",
                                },
                                "security_policy": {
                                    "ignore_https_errors": False,
                                    "robots_check": "required",
                                    "redirects": "same_host_and_allowlisted_path_only",
                                },
                                "fanout": {
                                    "capture_engine": "Crawl4AI",
                                    "declared_source_count": 2,
                                    "declared_source_ids": [
                                        "whoscored_public_pages",
                                        "laliga_public_pages",
                                    ],
                                    "pages_by_source": {
                                        "whoscored_public_pages": 1,
                                        "laliga_public_pages": 1,
                                    },
                                    "unidentified_page_count": 0,
                                },
                                "execution": {
                                    "strategy": "bounded_cross_host_parallel_per_host_serial",
                                    "host_group_count": 2,
                                    "max_host_workers": 2,
                                    "configured_source_count": 2,
                                    "duration_ms": 42.1259,
                                },
                            },
                        }
                    ],
                },
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {
                    "provider": "RSS",
                    "retrieved_at": "2026-08-17T05:00:00+00:00",
                    "feeds": [],
                },
            }

        def predictions(self) -> dict:
            return {
                "predictions": [
                    {
                        "fixture_id": "espn:1",
                        "competition_id": "csl",
                        "kickoff_at": "2026-08-18T11:35:00+00:00",
                        "home_team": "Home",
                        "away_team": "Away",
                        "primary_probability_1x2": {"home": 0.5, "draw": 0.25, "away": 0.25},
                        "scoreline_matrix": {"1-0": 0.5, "0-0": 0.5},
                    }
                ],
                "blocked": [],
            }

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    js_output = tmp_path / "offline_data.js"
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    js_output.write_bytes(b"previous-safe-full")
    compact_output.parent.mkdir(parents=True, exist_ok=True)
    compact_output.write_bytes(b"previous-safe-compact")

    with pytest.raises(
        ValueError,
        match=r"offline_bundle_source_provenance_missing:snapshot\.matches\[0\]",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "live.json",
            strict_report_path=report_path,
            output=js_output,
            compact_output=compact_output,
        )

    assert js_output.read_bytes() == b"previous-safe-full"
    assert compact_output.read_bytes() == b"previous-safe-compact"


def test_bundle_carries_publication_audit_without_turning_blocked_into_green(
    monkeypatch, tmp_path: Path
):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-20T00:00:00+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {"as_of": "2026-08-20T00:00:00+00:00", "source_registry": []},
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "publication-audit-current.json").write_text(
        json.dumps(
            {
                "status": "blocked",
                "checked_at": "2026-08-20T00:01:00+00:00",
                "public_url": "https://example.test",
                "errors": ["model_lock_mismatch", "public_snapshot_behind_local"],
                "checks": {
                    "public_read_model_reachable": True,
                    "model_lock_match": False,
                    "snapshot_time_comparable": True,
                    "snapshot_lag_seconds": 3600,
                    "snapshot_within_lag_budget": False,
                    "production_allowed_by_public_gate": False,
                },
                "public_preflight": {
                    "status": "stale_or_incomplete_routes",
                    "reachable": True,
                    "deployment_ready": False,
                    "all_expected_routes_ok": False,
                    "failed_routes": ["/api/v1/catalog"],
                },
                "deployment_required": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)

    compact_output = tmp_path / "offline_snapshot.json"
    build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=tmp_path / "live.json",
        strict_report_path=report_path,
        output=tmp_path / "offline_data.js",
        compact_output=compact_output,
        evidence_dir=evidence_dir,
    )

    audit = json.loads(compact_output.read_text(encoding="utf-8"))["publication_audit"]
    assert audit["status"] == "blocked"
    assert audit["checks"]["model_lock_match"] is False
    assert audit["checks"]["snapshot_lag_seconds"] == 3600
    assert audit["checks"]["public_contract_routes_ok"] is False
    assert audit["public_preflight"]["status"] == "stale_or_incomplete_routes"
    assert audit["public_preflight"]["failed_routes"] == ["/api/v1/catalog"]
    assert audit["deployment_required"] is True


def test_bundle_rejects_research_prediction_without_source_provenance(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return _bundle_snapshot(matches=[_trusted_openfootball_fixture()])

        def public_predictions(self, *, include_research_drafts=False) -> dict:
            result = {
                "status": "research_only",
                "predictions": [],
                "blocked": [],
            }
            if include_research_drafts:
                result["research_predictions"] = {
                    "status": "research_only",
                    "as_of": "2026-08-18T05:48:19+00:00",
                    "predictions": [
                        {
                            "fixture_id": "espn:research-1",
                            "competition_id": "csl",
                            "kickoff_at": "2026-08-19T11:35:00+00:00",
                            "home_team": "Home",
                            "away_team": "Away",
                            "as_of": "2026-08-18T05:48:19+00:00",
                            "cutoff_at": "2026-08-18T05:48:19+00:00",
                            "model_version": "draft-v1",
                            "status": "research_only",
                            "quality_gate": "blocked_for_production",
                            "team_identities": {
                                "home": {
                                    "input_name": "Coventry City",
                                    "canonical_name": "Coventry",
                                    "team_id": "premier-league:coventry",
                                    "alias_applied": True,
                                },
                                "away": {
                                    "input_name": "Hull",
                                    "canonical_name": "Hull",
                                    "team_id": "premier-league:hull",
                                    "alias_applied": False,
                                },
                            },
                            "coverage": {"ratio": 0.4, "missing": ["lineups"]},
                            "primary_probability_1x2": {"home": 0.5, "draw": 0.25, "away": 0.25},
                            "scoreline_matrix": {"1-0": 0.5, "0-0": 0.5},
                        }
                    ],
                    "blocked": [],
                }
            return result

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    compact_output = tmp_path / "offline_snapshot.json"
    output = tmp_path / "offline_data.js"
    output.write_bytes(b"previous-safe-full")
    compact_output.write_bytes(b"previous-safe-compact")

    with pytest.raises(
        ValueError,
        match=r"offline_bundle_source_provenance_missing:research_predictions\.predictions\[0\]",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "live.json",
            strict_report_path=report_path,
            output=output,
            compact_output=compact_output,
        )

    assert output.read_bytes() == b"previous-safe-full"
    assert compact_output.read_bytes() == b"previous-safe-compact"


def test_bundle_rejects_factual_rows_disguised_as_prediction_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore(_MinimalBundleStore):
        def public_predictions(self, *, include_research_drafts: bool = False) -> dict:
            result: dict[str, Any] = {
                "predictions": [],
                "blocked": [
                    {
                        "fixture_id": "openfootball:premier-league:1001",
                        "reason": "missing_model_input",
                    }
                ],
            }
            if include_research_drafts:
                result["research_predictions"] = {
                    "status": "unavailable",
                    "predictions": [],
                    "blocked": [],
                }
            return result

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline_full.js"
    output.write_bytes(b"previous-safe-full")

    with pytest.raises(
        ValueError,
        match=(
            r"offline_bundle_diagnostic_contains_factual_row:"
            r"predictions\.blocked\[0\]\.fixture_id"
        ),
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
        )

    assert output.read_bytes() == b"previous-safe-full"


def test_research_coverage_keeps_blocked_and_sales_only_reasons_separate():
    snapshot = {
        "matches": [
            {"id": "blocked", "competition_id": "csl", "status": "upcoming"},
            {"id": "sales", "competition_id": "cup", "status": "upcoming", "sales_only": True},
            {"id": "missing", "competition_id": "csl", "status": "upcoming"},
            {"id": "finished", "competition_id": "csl", "status": "finished"},
        ]
    }
    coverage = build_offline_bundle._research_prediction_coverage(
        snapshot,
        {
            "status": "research_only",
            "as_of": "2026-08-18T00:00:00+00:00",
            "predictions": [],
            "blocked": [{"fixture_id": "blocked", "reason": "insufficient_promoted_team_history"}],
        },
    )
    assert coverage["total_fixtures"] == 3
    assert coverage["model_scope_fixtures"] == 2
    assert coverage["available"] == 0
    assert coverage["blocked"] == 1
    assert coverage["not_eligible"] == 1
    assert coverage["unavailable"] == 1
    assert coverage["coverage_ratio"] == 0.0
    assert coverage["coverage_bands"] == {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    assert coverage["non_low_available"] == 0
    assert coverage["non_low_coverage_ratio"] == 0.0
    assert coverage["by_reason"] == {
        "insufficient_promoted_team_history": 1,
        "no_research_prediction_record": 1,
    }


def test_compact_summary_separates_blocked_diagnostics_from_fixture_coverage():
    snapshot = _bundle_snapshot(
        matches=[
            {
                "id": "available",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-28T12:00:00+00:00",
            },
            {
                "id": "blocked",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-28T13:00:00+00:00",
            },
            {
                "id": "unavailable",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-28T14:00:00+00:00",
            },
        ],
    )
    compact = build_offline_bundle._compact_site_snapshot(
        snapshot,
        {"predictions": [], "blocked": []},
        research_predictions={
            "status": "research_only",
            "as_of": "2026-08-25T00:00:00+00:00",
            "predictions": [
                {"fixture_id": "available", "coverage": {"level": "low"}},
            ],
            "blocked": [{"fixture_id": "blocked", "reason": "missing_history"}],
            "blocked_total": 7,
        },
    )

    summary = compact["research_prediction_summary"]
    assert summary["prediction_count"] == 1
    assert summary["blocked_count"] == 7
    assert summary["blocked_diagnostic_count"] == 7
    assert summary["blocked_diagnostic_sample_count"] == 1
    assert summary["available_fixture_count"] == 1
    assert summary["blocked_fixture_count"] == 1
    assert summary["unavailable_fixture_count"] == 1
    assert summary["model_scope_fixture_count"] == 3
    assert summary["coverage_ratio"] == round(1 / 3, 6)
    assert summary["blocked_count_semantics"] == "diagnostic_records_not_fixture_count"


def test_research_coverage_exposes_rolling_windows_without_changing_catalog_denominator():
    snapshot = {
        "generated_at": "2026-08-25T12:12:13+00:00",
        "matches": [
            {
                "id": "inside-3d",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": "2026-08-27T12:00:00+00:00",
            },
            {
                "id": "inside-7d",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-30T12:00:00+00:00",
            },
            {
                "id": "sales-7d",
                "competition_id": "cup",
                "status": "upcoming",
                "kickoff_at": "2026-08-29T12:00:00+00:00",
                "sales_only": True,
            },
            {
                "id": "outside-7d",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": "2026-09-04T12:00:00+00:00",
            },
        ],
    }
    coverage = build_offline_bundle._research_prediction_coverage(
        snapshot,
        {
            "status": "research_only",
            "as_of": "2026-08-25T12:00:00+00:00",
            "predictions": [
                {"fixture_id": "inside-3d", "coverage": {"level": "low"}},
            ],
            "blocked": [],
        },
    )

    # The complete catalog remains available for audit and is intentionally
    # not silently replaced by a rolling-window denominator.
    assert coverage["total_fixtures"] == 4
    assert coverage["model_scope_fixtures"] == 3
    assert coverage["available"] == 1
    assert coverage["coverage_ratio"] == round(1 / 3, 6)

    windows = coverage["windows"]
    assert coverage["window_reference_at"] == "2026-08-25T12:00:00Z"
    assert windows["3d"]["total_fixtures"] == 1
    assert windows["3d"]["model_scope_fixtures"] == 1
    assert windows["3d"]["available"] == 1
    assert windows["3d"]["coverage_ratio"] == 1.0
    assert windows["7d"]["total_fixtures"] == 3
    assert windows["7d"]["model_scope_fixtures"] == 2
    assert windows["7d"]["available"] == 1
    assert windows["7d"]["not_eligible"] == 1
    assert windows["7d"]["coverage_ratio"] == 0.5
    assert coverage["public_window"] == "7d"
    assert coverage["public_window_total_fixtures"] == 3


def test_bundle_rejects_sports_lottery_sales_schedule_from_static_output(
    monkeypatch, tmp_path: Path
):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-18T05:48:19+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [{"id": "premier-league", "model_health": {}}],
                "matches": [],
                "current_data": {
                    "as_of": "2026-08-18T05:48:19+00:00",
                    "source_registry": [],
                    "lottery_sales_schedule": [
                        {
                            "match_id": "lot-uefa-1",
                            "match_num": "周二001",
                            "league": "欧冠",
                            "competition_id": None,
                            "fixture_id": None,
                            "kickoff_at": "2026-08-19T18:00:00+00:00",
                            "home_team": "Sales Home",
                            "away_team": "Sales Away",
                            "link_status": "quarantined",
                            "link_reason": "unknown_competition_label",
                            "had_odds": {"h": 2.1, "d": 3.2, "a": 3.0},
                            "had_probability": {"h": 0.45, "d": 0.25, "a": 0.30},
                            "source": {
                                "name": "Sports Lottery",
                                "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
                                "retrieved_at": "2026-08-18T05:00:00+00:00",
                                "raw_sha256": "a" * 64,
                            },
                        }
                    ],
                },
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    output = tmp_path / "offline_data.js"
    compact_output = tmp_path / "offline_snapshot.json"
    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    output.write_bytes(b"previous-safe-full")
    compact_output.write_bytes(b"previous-safe-compact")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:sports_lottery_official:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "live.json",
            strict_report_path=report_path,
            output=output,
            compact_output=compact_output,
        )

    assert output.read_bytes() == b"previous-safe-full"
    assert compact_output.read_bytes() == b"previous-safe-compact"


def test_bundle_rejects_mixed_blocked_fixture_evidence(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-17T05:48:19+00:00",
                "timezone": "Asia/Shanghai",
                "summary": {},
                "competitions": [],
                "matches": [_trusted_openfootball_fixture()],
                "current_data": {"as_of": "2026-08-17T05:48:19+00:00", "source_registry": []},
                "espn_markets": {
                    "markets": [
                        {
                            "fixture_id": "espn:1",
                            "provider": "Public market",
                            "american_odds": {"home": 100},
                            "probability": {"home": 0.5},
                            "retrieved_at": "2026-08-17T05:00:00+00:00",
                            "source": {
                                "name": "Public market",
                                "url": "https://example.test/market",
                                "raw_sha256": "a" * 64,
                            },
                        }
                    ],
                    "team_status": [
                        {
                            "fixture_id": "espn:1",
                            "retrieved_at": "2026-08-17T05:01:00+00:00",
                            "confirmed": False,
                            "teams": {
                                "home": {"provider_team_id": "h", "name": "Home", "players": []},
                                "away": {"provider_team_id": "a", "name": "Away", "players": []},
                            },
                            "source": {
                                "name": "Roster source",
                                "url": "https://example.test/roster",
                            },
                        }
                    ],
                    "incidents": [
                        {
                            "fixture_id": "espn:1",
                            "retrieved_at": "2026-08-18T12:00:00+00:00",
                            "events": [
                                {
                                    "type": "Goal",
                                    "minute": "10'",
                                    "text": "Goal",
                                    "participants": ["Forward", "Creator"],
                                    "participant_ids": ["321058", 321058, True, {"bad": "id"}],
                                    "score": {"home": 1, "away": 0},
                                    "location": {
                                        "coordinate_system": "espn_field_percent",
                                        "source": "provider_declared",
                                        "provider_declared": True,
                                        "origin": {"x": 96.7, "y": 50},
                                        "target": {"x": 100, "y": 49},
                                        "goal_y": 49,
                                    },
                                    "period": 2,
                                    "scoring_play": True,
                                    "wallclock": "2026-08-18T12:45:00Z",
                                }
                            ],
                            "source": {
                                "name": "Match centre",
                                "url": "https://example.test/events",
                            },
                        }
                    ],
                    "match_stats": [
                        {
                            "fixture_id": "espn:1",
                            "retrieved_at": "2026-08-18T12:30:00+00:00",
                            "teams": {
                                "home": {"name": "Home", "statistics": []},
                                "away": {"name": "Away", "statistics": []},
                            },
                            "source": {
                                "name": "Match centre",
                                "url": "https://example.test/stats",
                            },
                        }
                    ],
                },
                "weather": {
                    "weather": [
                        {
                            "fixture_id": "espn:1",
                            "kickoff_at": "2026-08-18T11:35:00+00:00",
                            "temperature_c": 28,
                            "source": {
                                "name": "Open-Meteo",
                                "url": "https://example.test/weather",
                                "retrieved_at": "2026-08-17T05:02:00+00:00",
                            },
                        }
                    ],
                },
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {
                    "provider": "RSS",
                    "retrieved_at": "2026-08-17T05:00:00+00:00",
                    "feeds": [
                        {
                            "name": "Reliable RSS",
                            "url": "https://example.test/rss",
                            "retrieved_at": "2026-08-17T05:00:00+00:00",
                            "raw_sha256": "c" * 64,
                            "items": [
                                {
                                    "title": "Home vs Away preview and team news",
                                    "summary": "A pre-match preview for Home and Away.",
                                    "link": "https://example.test/home-away",
                                    "published_at": "2026-08-17T04:00:00+00:00",
                                },
                                {
                                    "title": "Unrelated public football story",
                                    "summary": "This does not name both teams.",
                                    "link": "https://example.test/unrelated",
                                    "published_at": "2026-08-17T04:00:00+00:00",
                                },
                            ],
                        }
                    ],
                },
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    compact_output = tmp_path / "public" / "offline_snapshot.json"
    output = tmp_path / "offline_data.js"
    output.write_bytes(b"previous-safe-full")
    compact_output.parent.mkdir(parents=True, exist_ok=True)
    compact_output.write_bytes(b"previous-safe-compact")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_market_summary:redistribution",
    ):
        build_offline_bundle.build_bundle(
            data_dir=tmp_path / "data",
            live_path=tmp_path / "missing-live.json",
            strict_report_path=report_path,
            output=output,
            compact_output=compact_output,
        )

    assert output.read_bytes() == b"previous-safe-full"
    assert compact_output.read_bytes() == b"previous-safe-compact"


def test_bundle_carries_empty_rights_blocked_live_overlay_diagnostic(monkeypatch, tmp_path: Path):
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-21T12:00:00+00:00",
                "timezone": "UTC",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {"as_of": "2026-08-21T12:00:00+00:00", "source_registry": []},
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    live_overlay_path = tmp_path / "live_overlay.json"
    live_overlay_path.write_text(
        json.dumps(
            {
                "schema_version": "matchline.live_overlay.v1",
                "status": "rights_blocked",
                "as_of": "2026-08-21T12:00:30+00:00",
                "fixture_count": 0,
                "fixtures": [],
                "errors": [],
                "source": {
                    "name": "ESPN event summary",
                    "rights_status": "blocked_pending_express_written_permission",
                },
                "policy": {"model_eligible": False, "network_opened": False},
            }
        ),
        encoding="utf-8",
    )
    compact_output = tmp_path / "public" / "offline_snapshot.json"

    build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=tmp_path / "missing-live.json",
        live_overlay_path=live_overlay_path,
        strict_report_path=report_path,
        output=tmp_path / "offline_data.js",
        compact_output=compact_output,
    )

    compact = json.loads(compact_output.read_text(encoding="utf-8"))
    assert compact["live_overlay"]["schema_version"] == "matchline.live_overlay.v1"
    assert compact["live_overlay"]["status"] == "rights_blocked"
    assert compact["live_overlay"]["fixtures"] == []
    assert compact["live_overlay"]["policy"]["network_opened"] is False
    bundle_text = (tmp_path / "offline_data.js").read_text(encoding="utf-8")
    assert '"schema_version":"matchline.live_overlay.v1"' in bundle_text


def test_compact_snapshot_preserves_bounded_source_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self) -> dict:
            return {
                "generated_at": "2026-08-21T13:20:21+00:00",
                "timezone": "UTC",
                "summary": {},
                "competitions": [],
                "matches": [],
                "current_data": {
                    "as_of": "2026-08-21T13:20:21+00:00",
                    "source_registry": [],
                    "canonical_fixture_provider": "OpenFootball",
                    "canonical_fixture_count": 65,
                    "espn_fixture_count": 0,
                    "crawl4ai_status": "degraded",
                    "crawl4ai_whoscored_joined_count": 3,
                    "espn_injury_report_count": 4,
                    "espn_injury_player_count": 7,
                    "espn_injury_complete_fixture_count": 2,
                    "espn_injury_partial_fixture_count": 1,
                    "premier_league_official_status": "ok",
                    "premier_league_official_fixture_count": 8,
                    "premier_league_official_lineup_count": 2,
                    "laliga_official_status": "degraded",
                    "laliga_official_fixture_count": 7,
                    "laliga_official_lineup_count": 3,
                    "laliga_official_confirmed_lineup_count": 1,
                    "laliga_official_not_published_count": 2,
                    "bundesliga_official_status": "not_requested",
                    "bundesliga_official_fixture_count": 6,
                    "bundesliga_official_lineup_count": 0,
                    "serie_a_official_status": "ok",
                    "serie_a_official_fixture_count": 5,
                    "serie_a_official_lineup_count": 1,
                    "ligue1_official_status": "ok",
                    "ligue1_official_fixture_count": 1,
                    "ligue1_official_lineup_count": 1,
                    "ligue1_official_confirmed_lineup_count": 0,
                    "ligue1_official_not_published_count": 1,
                    "internal_only_sentinel": "must_not_leak",
                },
                "strict_backtest": {"status": "stale"},
                "prospective_evaluation": {"status": "pending"},
                "news": {"provider": "RSS", "feeds": []},
            }

        def predictions(self) -> dict:
            return {"predictions": [], "blocked": []}

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}), encoding="utf-8"
    )
    compact_output = tmp_path / "public" / "offline_snapshot.json"

    build_offline_bundle.build_bundle(
        data_dir=tmp_path / "data",
        live_path=tmp_path / "missing-live.json",
        strict_report_path=report_path,
        output=tmp_path / "offline_data.js",
        compact_output=compact_output,
    )

    current_data = json.loads(compact_output.read_text(encoding="utf-8"))["current_data"]
    assert current_data["canonical_fixture_provider"] == "OpenFootball"
    assert current_data["canonical_fixture_count"] == 65
    assert current_data["espn_fixture_count"] == 0
    expected_diagnostics = {
        "crawl4ai_status": "degraded",
        "crawl4ai_whoscored_joined_count": 3,
        "espn_injury_report_count": 4,
        "espn_injury_player_count": 7,
        "espn_injury_complete_fixture_count": 2,
        "espn_injury_partial_fixture_count": 1,
        "premier_league_official_status": "ok",
        "premier_league_official_fixture_count": 8,
        "premier_league_official_lineup_count": 2,
        "laliga_official_status": "degraded",
        "laliga_official_fixture_count": 7,
        "laliga_official_lineup_count": 3,
        "laliga_official_confirmed_lineup_count": 1,
        "laliga_official_not_published_count": 2,
        "bundesliga_official_status": "not_requested",
        "bundesliga_official_fixture_count": 6,
        "bundesliga_official_lineup_count": 0,
        "serie_a_official_status": "ok",
        "serie_a_official_fixture_count": 5,
        "serie_a_official_lineup_count": 1,
        "ligue1_official_status": "ok",
        "ligue1_official_fixture_count": 1,
        "ligue1_official_lineup_count": 1,
        "ligue1_official_confirmed_lineup_count": 0,
        "ligue1_official_not_published_count": 1,
    }
    assert {key: current_data[key] for key in expected_diagnostics} == expected_diagnostics
    assert "internal_only_sentinel" not in current_data


def test_compact_snapshot_keeps_aggregate_lineup_rights_state_without_fixture_plans() -> None:
    snapshot = _bundle_snapshot()
    raw_sources = {
        "lineup_poll_diagnostics": {
            "la-liga": {
                "state": "awaiting_publication",
                "reason_code": "source_polled_without_confirmed_xi",
                "provider": "LaLiga official",
                "source_key": "laliga_official",
                "source_status": "rights_blocked",
                "candidate_count": 3,
                "fixture_plans": [
                    {
                        "fixture_id": "openfootball:la-liga:1",
                        "kickoff_at": "2026-08-28T18:30:00+00:00",
                        "poll_due": True,
                    }
                ],
            }
        },
        "laliga_official": {
            "status": "rights_blocked",
            "rights_status": "blocked_rights_not_verified",
            "network_opened": False,
            "fixtures": [],
            "lineups": [],
            "errors": [],
        },
    }

    compact = build_offline_bundle._compact_site_snapshot(
        snapshot,
        {"predictions": [], "blocked": []},
        raw_sources=raw_sources,
    )
    diagnostic = compact["current_data"]["lineup_poll_diagnostics"]["la-liga"]
    assert diagnostic["state"] == "rights_blocked"
    assert diagnostic["source_status"] == "rights_blocked"
    assert diagnostic["rights_status"] == "blocked_rights_not_verified"
    assert diagnostic["network_opened"] is False
    assert diagnostic["candidate_count"] == 3
    assert "fixture_plans" not in diagnostic


def test_crawl4ai_fixture_evidence_keeps_provider_and_page_lineage():
    compact = build_offline_bundle._compact_fixture_evidence(
        {
            "crawl4ai": {
                "pages": [
                    {
                        "source": "Premier League official public match pages operator source",
                        "source_id": "premier_league_public_pages",
                        "fact_source_id": "premier_league_public_pages",
                        "source_tier": "official",
                        "capture_engine": "Crawl4AI",
                        "capture_role": "fetch_runtime",
                        "source_role": "fact_source",
                        "crawl_stage": "detail",
                        "parent_url": "https://www.premierleague.com/en/matches",
                        "parent_content_sha256": "a" * 64,
                        "follow_reason": "match_url",
                        "parser_contract": "premier_league_public_fixtures_v1",
                        "url": "https://www.premierleague.com/en/match/1/detail",
                        "final_url": "https://www.premierleague.com/en/match/1/detail",
                        "content_sha256": "b" * 64,
                        "observed_at": "2026-08-21T02:00:00+00:00",
                        "joined_fixture_records": [
                            {
                                "join_status": "exact",
                                "fixture_id": "espn:1",
                                "source_parser": "premier_league_public_fixtures_v1",
                                "source_match_id": "1",
                                "match_url": "https://www.premierleague.com/en/match/1/detail",
                                "canonical_home_team": "Home",
                                "canonical_away_team": "Away",
                                "source_kickoff_at": "2026-08-22T12:00:00+00:00",
                                "canonical_kickoff_at": "2026-08-22T12:00:00+00:00",
                                "kickoff_delta_seconds": 0,
                                "join_confidence": 0.99,
                                "admission": {
                                    "status": "review_required",
                                    "reason_code": "source_license_review_required",
                                },
                            }
                        ],
                    }
                ],
            },
        },
        [{"id": "espn:1", "kickoff_at": "2026-08-22T12:00:00+00:00"}],
    )

    context = compact["espn:1"]["intelligence"][0]
    assert context["captureEngine"] == "Crawl4AI"
    assert context["sourceId"] == "premier_league_public_pages"
    assert context["factSourceId"] == "premier_league_public_pages"
    assert context["crawlStage"] == "detail"
    assert context["parentUrl"].endswith("/en/matches")
    assert context["parentContentSha256"] == "a" * 64
    assert context["contentSha256"] == "b" * 64
    assert context["parserContract"] == "premier_league_public_fixtures_v1"
    timeline = compact["espn:1"]["timeline"]
    assert any(
        row.get("crawlStage") == "detail" and row.get("captureEngine") == "Crawl4AI"
        for row in timeline
    )


def test_openfootball_canonical_fixture_has_public_source_lineage_evidence():
    fixture_id = "openfootball:premier-league:abc123"
    evidence = build_offline_bundle._compact_fixture_evidence(
        {
            "fixture_feed": {
                "provider": "OpenFootball",
                "fixtures": [
                    {
                        "id": fixture_id,
                        "competition_id": "premier-league",
                        "kickoff_at": "2026-08-24T19:00:00+00:00",
                        "home_team": "Home",
                        "away_team": "Away",
                        "source": {
                            "name": "OpenFootball",
                            "source_id": "openfootball:football.json:2026-27:en.1",
                            "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
                            "retrieved_at": "2026-08-24T01:00:00+00:00",
                            "raw_sha256": "a" * 64,
                            "license": "CC0-1.0",
                        },
                    }
                ],
                "errors": [],
            }
        },
        [
            {
                "id": fixture_id,
                "competition_id": "premier-league",
                "kickoff_at": "2026-08-24T19:00:00+00:00",
            }
        ],
    )

    row = evidence[fixture_id]["intelligence"][0]
    assert row["kind"] == "canonical_fixture_schedule"
    assert row["sourceName"] == "OpenFootball"
    assert row["sourceId"] == "openfootball:football.json:2026-27:en.1"
    assert row["sourceLicense"] == "CC0-1.0"
    assert row["rawHash"] == "a" * 64
    assert row["entersModel"] is False
    assert evidence[fixture_id]["timeline"][0]["kind"] == "canonical_fixture_schedule"


def test_official_lineup_projection_emits_one_timeline_observation_for_both_team_rows():
    fixture_id = "openfootball:la-liga:fixture-lineup"
    source = {
        "name": "LaLiga official",
        "url": "https://www.laliga.com/en-GB/match/example",
        "retrieved_at": "2026-08-24T19:05:51+00:00",
        "raw_sha256": "b" * 64,
    }
    evidence = build_offline_bundle._compact_fixture_evidence(
        {
            "fixture_feed": {
                "provider": "OpenFootball",
                "fixtures": [
                    {
                        "id": fixture_id,
                        "competition_id": "la-liga",
                        "kickoff_at": "2026-08-25T19:00:00+00:00",
                        "home_team": "Home",
                        "away_team": "Away",
                        "source": {
                            "name": "OpenFootball",
                            "retrieved_at": "2026-08-24T18:00:00+00:00",
                        },
                    }
                ],
            },
            "laliga_official": {
                "provider": "LaLiga official",
                "retrieved_at": source["retrieved_at"],
                "lineups": [
                    {
                        "fixture_id": fixture_id,
                        "official_fixture": {
                            "home_team": "Home",
                            "away_team": "Away",
                            "source": source,
                        },
                        "lineups": {
                            "confirmed": False,
                            "home": {"players": []},
                            "away": {"players": []},
                        },
                        "source": source,
                    }
                ],
            },
        },
        [
            {
                "id": fixture_id,
                "competition_id": "la-liga",
                "kickoff_at": "2026-08-25T19:00:00+00:00",
            }
        ],
    )

    assert len(evidence[fixture_id]["lineups"]) == 2
    lineup_timeline = [
        row for row in evidence[fixture_id]["timeline"] if row["category"] == "lineup"
    ]
    assert len(lineup_timeline) == 1
    assert lineup_timeline[0]["kind"] == "official_lineup_not_published"
