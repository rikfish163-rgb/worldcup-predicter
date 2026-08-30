from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_sites_publish_service_is_secret_free_and_uses_the_causal_cycle():
    service = (SYSTEMD / "matchline-sites-publish.service").read_text(encoding="utf-8")

    assert "ConditionPathExists=/etc/matchline/matchline-sites.env" in service
    assert "EnvironmentFile=-/etc/matchline/matchline-sites.env" in service
    assert (
        "RequiresMountsFor=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime" in service
    )
    assert "ExecStart=/usr/bin/python3 -m league_platform.publish_cycle" in service
    assert ".venv/bin/python" not in service
    assert "--stage-lock ${MATCHLINE_PROSPECTIVE_LOCK}" in service
    assert "--cycle-evidence ${MATCHLINE_RUNTIME_DIR}/prospective-cycle-latest.json" in service
    assert "--max-snapshot-age-seconds 1800" in service
    assert "--require-publication-diagnostic" in service
    for required in (
        "--maturity-strict-report ${MATCHLINE_RUNTIME_DIR}/strict-backtest-current.json",
        "--maturity-evaluation ${MATCHLINE_RUNTIME_DIR}/prospective-evaluation-current.json",
        "--maturity-prediction-archive ${MATCHLINE_RUNTIME_DIR}/prospective_predictions.jsonl",
        "--maturity-sites-build-evidence /home/hetaisheng/soccerdata/matchline_sites/.runtime/sites-build-resource.json",
        "--maturity-test-evidence /home/hetaisheng/soccerdata/docs/evidence/platform-test-evidence-current.json",
        "--maturity-publication-audit /home/hetaisheng/soccerdata/docs/evidence/publication-audit-current.json",
    ):
        assert required in service
    assert (
        "Environment=MATCHLINE_RUNTIME_DIR=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime"
        in service
    )
    assert "/home/hetaisheng/soccerdata/data/live" not in service
    assert "MATCHLINE_INGEST_TOKEN=" not in service
    assert "--dry-run" not in service


def test_sites_publish_enforces_current_three_hash_gate_before_any_write():
    service = (SYSTEMD / "matchline-sites-publish.service").read_text(encoding="utf-8")
    generator = "ExecStartPre=/usr/bin/python3 -m league_platform.platform_verification"
    enforcement = "ExecStartPre=/usr/bin/python3 -m league_platform.maturity_validator"
    publisher = "ExecStart=/usr/bin/python3 -m league_platform.publish_cycle"

    assert generator in service
    assert enforcement in service
    assert service.index(generator) < service.index(enforcement) < service.index(publisher)
    assert "--strict-report ${MATCHLINE_RUNTIME_DIR}/strict-backtest-current.json" in service
    assert (
        "--platform-verification ${MATCHLINE_RUNTIME_DIR}/platform-verification-current.json"
        in service
    )
    assert "--require-passed" in service
    assert "platform-maturity-verification-2026-08-14-v16-latest.json" not in service
    for required in (
        "--live-snapshot ${MATCHLINE_RUNTIME_DIR}/current.json",
        "--publication-diagnostic ${MATCHLINE_RUNTIME_DIR}/current.publication.json",
        "--runtime-root ${MATCHLINE_RUNTIME_DIR}",
        "--test-evidence-root /home/hetaisheng/soccerdata/docs/evidence",
        "--code-root /home/hetaisheng/soccerdata",
        "--cycle ${MATCHLINE_RUNTIME_DIR}/prospective-cycle-latest.json",
        "--offline-snapshot ${MATCHLINE_RUNTIME_DIR}/offline_snapshot.json",
        "--repository-root /home/hetaisheng/soccerdata",
    ):
        assert required in service


def test_sites_publish_timer_is_persistent_and_bounded():
    timer = (SYSTEMD / "matchline-sites-publish.timer").read_text(encoding="utf-8")

    assert "OnUnitInactiveSec=15min" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "Persistent=true" in timer
    assert "Unit=matchline-sites-publish.service" in timer


def test_sites_env_example_contains_all_required_endpoints_without_a_real_secret():
    env = (SYSTEMD / "matchline-sites.env.example").read_text(encoding="utf-8")

    for key in (
        "MATCHLINE_FIXTURES_REGISTER_URL=",
        "MATCHLINE_INGEST_URL=",
        "MATCHLINE_FORECAST_REGISTER_URL=",
        "MATCHLINE_FORECAST_URL=",
        "MATCHLINE_PUBLICATION_EPOCH=",
        "MATCHLINE_PRODUCER_KEY_ID=",
        "MATCHLINE_PRODUCER_SIGNING_SECRET=",
        "MATCHLINE_INGEST_TOKEN=replace-with-a-random-runtime-secret",
    ):
        assert key in env
    assignments = [line for line in env.splitlines() if line and not line.startswith("#")]
    assert all("Bearer " not in line for line in assignments)
    assert "TUoV-" not in env


def test_sites_runtime_example_follows_the_prospective_cycle_root():
    text = (SYSTEMD / "matchline-runtime.env.example").read_text(encoding="utf-8")
    expected = "MATCHLINE_RUNTIME_DIR=/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime"
    assert expected in text
    assert "/home/hetaisheng/soccerdata/data/live" not in text


def test_prospective_cycle_mirrors_runtime_pointers_around_runtime_only_bundle():
    service = (SYSTEMD / "matchline-prospective-cycle.service").read_text(encoding="utf-8")
    runtime = "/usr/bin/python3 -m league_platform.runtime_evidence --runtime-dir=${MATCHLINE_RUNTIME_DIR}"
    bundle = "/usr/bin/python3 -m league_platform.build_offline_bundle"
    maturity = "/usr/bin/python3 -m league_platform.maturity_validator"

    first_runtime = service.index(runtime)
    bundle_at = service.index(bundle)
    second_runtime = service.index(runtime, first_runtime + 1)
    maturity_at = service.index(maturity)

    # The first mirror exposes the completed cycle/evaluation pointers to the
    # runtime projection; the second records the resulting runtime-only bundle.
    # A separate read-model lane owns any local Sites rebuild.
    assert first_runtime < bundle_at < second_runtime < maturity_at
    assert "run-matchline-sites-build.sh" not in service
    assert "matchline_sites/public/offline_snapshot.json" not in service
    assert "--enable-wikidata" in service
    # The prospective cycle only produces audit evidence.  A pending window,
    # first-run missing bundle, or honest maturity block must not prevent the
    # later bundle/platform receipts from being generated.  Enforcement belongs
    # exclusively to the Sites publication service before its first write.
    assert "--require-passed" not in service
    assert service.count("--lock=${MATCHLINE_PROSPECTIVE_LOCK}") >= 2
    for required in (
        "--live-snapshot ${MATCHLINE_RUNTIME_DIR}/current.json",
        "--publication-diagnostic ${MATCHLINE_RUNTIME_DIR}/current.publication.json",
        "--runtime-root ${MATCHLINE_RUNTIME_DIR}",
        "--test-evidence-root /home/hetaisheng/soccerdata/docs/evidence",
        "--code-root /home/hetaisheng/soccerdata",
        "--cycle ${MATCHLINE_RUNTIME_DIR}/prospective-cycle-latest.json",
        "--offline-snapshot ${MATCHLINE_RUNTIME_DIR}/offline_snapshot.json",
        "--repository-root /home/hetaisheng/soccerdata",
    ):
        assert required in service


def test_sites_build_resource_wrapper_has_process_level_audit_contract():
    wrapper = (SYSTEMD / "run-matchline-sites-build.sh").read_text(encoding="utf-8")

    assert "set -u" in wrapper
    assert "prepare_node_modules_overlay" in wrapper
    assert "restore_node_modules" in wrapper
    assert "node_modules-external-link.$$" in wrapper
    assert 'mkdir -- "${node_modules_dir}/.vite-temp"' in wrapper
    assert "/usr/bin/time -v" in wrapper
    assert "sites-build-resource.json" in wrapper
    assert '"scope": "vinext_build_process_only"' in wrapper
    assert '"maximum_resident_set_size_kb"' in wrapper
    assert '"swaps"' in wrapper
    assert '"elapsed_wall_clock"' in wrapper
    assert "mv -f" in wrapper


def test_sites_build_resource_receipt_can_avoid_runtime_filesystem():
    wrapper = (SYSTEMD / "run-matchline-sites-build.sh").read_text(encoding="utf-8")

    assert "MATCHLINE_SITES_BUILD_RESOURCE_DIR" in wrapper
    assert (
        'resource_dir="${MATCHLINE_SITES_BUILD_RESOURCE_DIR:-${project_dir}/.runtime}"' in wrapper
    )
    assert 'resource_file="${resource_dir}/sites-build-resource.json"' in wrapper
    # The raw DR bundle is imported by the server only.  The build receipt must
    # verify that source/compiled server paths are present while refusing any
    # public/client copy that could bypass the Worker boundary.
    assert 'source_snapshot="${project_dir}/app/offline_snapshot.data.json"' in wrapper
    assert 'forbidden_snapshot_name="offline_snapshot.json"' in wrapper
    assert 'public_snapshot="${project_dir}/public/${forbidden_snapshot_name}"' in wrapper
    assert 'built_client_snapshot="${project_dir}/dist/client/${forbidden_snapshot_name}"' in wrapper
    assert 'worker_bundle="${project_dir}/dist/server/index.js"' in wrapper
    assert 'static_snapshot_not_public' in wrapper
    assert 'cmp -s "${source_snapshot}" "${built_snapshot}"' not in wrapper
    assert 'source_snapshot="${project_dir}/public/' not in wrapper
    assert 'built_snapshot=' not in wrapper
    assert "sha256sum" in wrapper
    assert (
        'export MATCHLINE_LOCAL_STATE_DIR="${MATCHLINE_LOCAL_STATE_DIR:-/dev/shm/'
        'matchline-sites-build-state}"'
    ) in wrapper


def test_readmodel_only_refreshes_runtime_artifacts():
    service = (SYSTEMD / "matchline-runtime-readmodel.service").read_text(encoding="utf-8")
    assert "--compact-output ${MATCHLINE_RUNTIME_DIR}/offline_snapshot.json" in service
    assert "run-matchline-sites-build.sh" not in service
    assert "matchline_sites/public/offline_snapshot.json" not in service
    assert "matchline-sites-local.service" not in service
