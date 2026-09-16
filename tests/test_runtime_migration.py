from pathlib import Path

import pytest

from league_platform.runtime_migration import apply_migration, plan_migration


def test_plan_inventories_regular_files_and_keeps_transient_locks_out(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "crawl4ai").mkdir(parents=True)
    (source / "current.json").write_text('{"as_of":"now"}\n', encoding="utf-8")
    (source / "crawl4ai" / "pages.jsonl.lock").write_text("stale", encoding="utf-8")
    (source / "crawl4ai" / "pages.jsonl").write_text("{}\n", encoding="utf-8")

    plan = plan_migration(source, target, min_free_bytes=0)

    assert plan.ready
    assert {entry.relative_path for entry in plan.entries} == {"current.json", "crawl4ai/pages.jsonl"}
    assert plan.skipped_transient == ("crawl4ai/pages.jsonl.lock",)
    assert plan.bytes_to_copy > 0


def test_apply_requires_explicit_operator_and_cycle_guards_and_preserves_source(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "current.json").write_text("source", encoding="utf-8")
    plan = plan_migration(source, target, min_free_bytes=0)

    with pytest.raises(PermissionError):
        apply_migration(plan, operator_approved=True, cycle_stopped=False)

    result = apply_migration(plan, operator_approved=True, cycle_stopped=True)
    assert result["status"] == "copied"
    assert result["source_preserved"] is True
    assert (source / "current.json").read_text(encoding="utf-8") == "source"
    assert (target / "current.json").read_text(encoding="utf-8") == "source"


def test_plan_rejects_nested_runtime_roots(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="inside source"):
        plan_migration(source, source / "moved", min_free_bytes=0)


def test_plan_blocks_missing_source_instead_of_creating_an_empty_runtime(tmp_path: Path):
    plan = plan_migration(tmp_path / "missing", tmp_path / "target", min_free_bytes=0)
    assert plan.source_status == "missing"
    assert not plan.ready


def test_plan_reports_non_writable_destination_before_apply(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    target.chmod(0o555)
    try:
        plan = plan_migration(source, target, min_free_bytes=0)
        assert not plan.destination_writable
        assert not plan.ready
        assert plan.as_dict()["destination_writable"] is False
    finally:
        target.chmod(0o755)
