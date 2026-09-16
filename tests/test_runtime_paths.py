from pathlib import Path

from league_platform.runtime_paths import resolve_runtime_paths


def test_runtime_paths_are_all_below_operator_root():
    paths = resolve_runtime_paths(Path("/mnt/matchline-runtime"))
    assert paths.root == Path("/mnt/matchline-runtime")
    assert paths.live_path == paths.root / "current.json"
    assert paths.archive_dir == paths.root / "archive"
    assert paths.intelligence_ledger == paths.root / "intelligence/observations.jsonl"
    assert (
        paths.intelligence_quarantine == paths.root / "intelligence/observations.quarantine.jsonl"
    )
    assert paths.prediction_archive == paths.root / "prospective_predictions.jsonl"
    assert paths.cycle_evidence == paths.root / "prospective-cycle-latest.json"
    assert paths.evaluation_evidence == paths.root / "prospective-evaluation-current.json"
    assert paths.crawl4ai_archive == paths.root / "crawl4ai"
    assert paths.openfootball_raw_archive_dir == paths.root / "openfootball-raw"
    assert paths.sites_receipts_dir == paths.root / "sites"
    for value in paths.__dict__.values():
        if isinstance(value, Path):
            value.relative_to(paths.root)


def test_runtime_paths_preserve_existing_default_layout():
    paths = resolve_runtime_paths()
    assert str(paths.live_path) == "data/live/current.json"
    assert str(paths.prediction_archive) == "data/live/prospective_predictions.jsonl"
    assert str(paths.cycle_evidence) == "data/live/prospective-cycle-latest.json"
    assert str(paths.openfootball_raw_archive_dir) == "data/live/openfootball-raw"
