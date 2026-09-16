from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from league_platform import app, build_offline_bundle, runtime_paths


_RAW_ARCHIVE_ENV = "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"


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


def _snapshot_with_research_schedule() -> dict[str, Any]:
    fixture = _trusted_openfootball_fixture()
    return {
        "generated_at": "2026-08-25T00:00:00+00:00",
        "timezone": "Asia/Shanghai",
        "summary": {},
        "competitions": [],
        "matches": [fixture],
        "current_data": {"status": "stale", "source_registry": []},
        "strict_backtest": {"status": "stale"},
        "prospective_evaluation": {"status": "pending"},
        "news": {"provider": "Public RSS", "feeds": []},
    }


def _unavailable_future() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "as_of": "2026-08-25T00:00:00+00:00",
        "predictions": [],
        "blocked": [],
        "reason": "verified_openfootball_archive_missing",
        "model_input": {
            "status": "unavailable",
            "trust_anchor": "verified_openfootball_raw_archive_replay",
            "snapshot_model_facts_accepted": False,
        },
        "model_health": {},
        "message": "verified raw archive unavailable",
    }


def test_future_archive_resolver_never_infers_relative_tmpfs_or_runtime_fallback() -> None:
    resolver = getattr(runtime_paths, "resolve_future_openfootball_raw_archive_dir", None)
    assert callable(resolver)

    durable_env = Path("/var/lib/matchline/openfootball-raw")
    assert resolver(None, environ={}) is None
    assert resolver(None, environ={"MATCHLINE_RUNTIME_DIR": "/srv/matchline"}) is None
    assert resolver(None, environ={_RAW_ARCHIVE_ENV: str(durable_env)}) == durable_env
    assert resolver(Path("data/live/openfootball-raw"), environ={}) is None
    assert resolver(Path("/dev/shm/matchline/openfootball-raw"), environ={}) is None
    assert (
        resolver(
            Path("relative/openfootball-raw"),
            environ={_RAW_ARCHIVE_ENV: str(durable_env)},
        )
        is None
    )


def test_app_server_routes_durable_archive_and_one_aware_cutoff_to_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, data_dir: Path, **options: Any) -> None:
            captured["data_dir"] = data_dir
            captured.update(options)

    monkeypatch.setattr(app, "PlatformStore", FakeStore)
    raw_archive = Path("/var/lib/matchline/openfootball-raw")

    server = app.create_server(
        "127.0.0.1",
        0,
        tmp_path / "history",
        live_path=tmp_path / "current.json",
        openfootball_raw_archive_dir=raw_archive,
    )
    try:
        assert captured["openfootball_raw_archive_dir"] == raw_archive
        observed_before = captured["now"]
        assert observed_before.tzinfo is not None
        assert observed_before.utcoffset() is not None
    finally:
        server.server_close()


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], Path("/var/lib/matchline/from-operator-env")),
        (
            ["--openfootball-raw-archive-dir", "/srv/matchline/from-cli"],
            Path("/srv/matchline/from-cli"),
        ),
    ],
)
def test_app_cli_routes_explicit_or_operator_archive_without_runtime_inference(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeServer:
        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            captured["closed"] = True

    def fake_create_server(**options: Any) -> FakeServer:
        captured.update(options)
        return FakeServer()

    monkeypatch.setenv(_RAW_ARCHIVE_ENV, "/var/lib/matchline/from-operator-env")
    monkeypatch.setattr(app, "create_server", fake_create_server)
    monkeypatch.setattr(sys, "argv", ["league_platform.app", *extra_args])

    app.main()

    assert captured["openfootball_raw_archive_dir"] == expected
    assert captured["closed"] is True


def test_app_research_endpoint_does_not_replay_self_reported_stale_snapshot() -> None:
    calls = {"public": 0, "future": 0}
    unavailable = _unavailable_future()

    class FakeStore:
        def public_predictions(self, *, include_research_drafts: bool = False) -> dict:
            calls["public"] += 1
            assert include_research_drafts is True
            return {
                "status": "research_only",
                "predictions": [],
                "blocked": [],
                "research_predictions": {
                    "status": "stale_snapshot",
                    "predictions": [{"fixture_id": "self-issued-old-snapshot"}],
                    "authorization_reference": "operator-self-issued",
                },
            }

        def predictions(self) -> dict[str, Any]:
            calls["future"] += 1
            return unavailable

    class Capture:
        path = "/api/v1/research-predictions"
        store = FakeStore()
        payload: object | None = None

        def _send_json(self, payload: object, *, status: int = 200) -> None:
            assert status == 200
            self.payload = payload

    capture = Capture()
    app.PlatformHandler.do_GET(capture)  # type: ignore[arg-type]

    assert calls == {"public": 0, "future": 1}
    assert isinstance(capture.payload, dict)
    assert capture.payload["status"] == "unavailable"
    assert capture.payload["predictions"] == []
    assert capture.payload["production_allowed"] is False
    assert capture.payload["research_predictions"] == unavailable


def test_offline_bundle_uses_direct_verified_future_and_keeps_schedule_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    calls: list[bool] = []

    class FakeStore:
        def __init__(self, data_dir: Path, **options: Any) -> None:
            captured["data_dir"] = data_dir
            captured.update(options)

        def snapshot(self) -> dict[str, Any]:
            return _snapshot_with_research_schedule()

        def public_predictions(self, *, include_research_drafts: bool = False) -> dict:
            calls.append(include_research_drafts)
            result: dict[str, Any] = {
                "status": "unavailable",
                "predictions": [],
                "blocked": [],
            }
            if include_research_drafts:
                result["research_predictions"] = {
                    "status": "stale_snapshot",
                    "predictions": [],
                    "blocked": [],
                    "authorization_reference": "operator-self-issued",
                    "poison_marker": "old-snapshot-fallback",
                }
            return result

        def predictions(self) -> dict[str, Any]:
            return _unavailable_future()

    monkeypatch.setattr(build_offline_bundle, "PlatformStore", FakeStore)
    monkeypatch.setenv(_RAW_ARCHIVE_ENV, "/var/lib/matchline/openfootball-raw")
    report_path = tmp_path / "strict.json"
    report_path.write_text(
        json.dumps({"leagues": {}, "combined": {"sample_n": 0}}),
        encoding="utf-8",
    )
    output = tmp_path / "offline.js"
    compact_output = tmp_path / "offline_snapshot.json"

    result = build_offline_bundle.build_bundle(
        data_dir=tmp_path / "history",
        live_path=tmp_path / "missing-current.json",
        strict_report_path=report_path,
        output=output,
        compact_output=compact_output,
    )

    assert calls == [False]
    assert captured["openfootball_raw_archive_dir"] == Path(
        "/var/lib/matchline/openfootball-raw"
    )
    observed_before = captured["now"]
    assert observed_before.tzinfo is not None
    assert observed_before.utcoffset() is not None
    assert result["prediction_count"] == 0
    assert result["research_prediction_count"] == 0
    compact = json.loads(compact_output.read_text(encoding="utf-8"))
    assert compact["matches"][0]["id"] == "openfootball:premier-league:1001"
    assert compact["research_prediction_summary"]["status"] == "unavailable"
    assert compact["research_prediction_summary"]["prediction_count"] == 0
    assert compact["research_prediction_summary"]["production_allowed"] is False
    assert "old-snapshot-fallback" not in output.read_text(encoding="utf-8")


def test_offline_cli_accepts_explicit_durable_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_bundle(**options: Any) -> dict[str, Any]:
        captured.update(options)
        return {"prediction_count": 0, "research_prediction_count": 0}

    monkeypatch.setattr(build_offline_bundle, "build_bundle", fake_build_bundle)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "league_platform.build_offline_bundle",
            "--openfootball-raw-archive-dir",
            "/srv/matchline/openfootball-raw",
        ],
    )

    build_offline_bundle.main()

    assert captured["openfootball_raw_archive_dir"] == Path(
        "/srv/matchline/openfootball-raw"
    )


def test_offline_cli_uses_the_single_current_lock_and_runtime_report_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_bundle(**options: Any) -> dict[str, Any]:
        captured.update(options)
        return {"prediction_count": 0, "research_prediction_count": 0}

    monkeypatch.setattr(build_offline_bundle, "build_bundle", fake_build_bundle)
    monkeypatch.setattr(
        sys,
        "argv",
        ["league_platform.build_offline_bundle"],
    )

    build_offline_bundle.main()

    assert captured["strict_report_path"].name == "strict-backtest-current.json"
    assert captured["prospective_lock_path"].name == "prospective-model-lock-current.json"


def test_systemd_readmodel_and_cycle_pass_the_operator_raw_archive() -> None:
    root = Path(__file__).parents[1]
    readmodel = (root / "deploy/systemd/matchline-runtime-readmodel.service").read_text(
        encoding="utf-8"
    )
    cycle = (root / "deploy/systemd/matchline-prospective-cycle.service").read_text(
        encoding="utf-8"
    )

    durable_environment = (
        "Environment=MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR=/media/hetaisheng/"
        "044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw"
    )
    archive_flag = (
        "--openfootball-raw-archive-dir "
        "${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR}"
    )
    assert durable_environment in readmodel
    assert archive_flag in readmodel
    assert archive_flag in cycle
