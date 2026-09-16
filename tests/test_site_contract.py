from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "league_platform" / "site" / "app.js"
HTML = ROOT / "league_platform" / "site" / "index.html"
APP_SERVER = ROOT / "league_platform" / "app.py"


def test_site_script_is_syntax_valid_and_low_coverage_is_not_rendered_as_probability():
    source = APP.read_text(encoding="utf-8")
    node = shutil.which("node")
    if node:
        result = subprocess.run(
            [node, "--check", str(APP)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert 'if (level === "low" || coverage.critical_conflict === true)' in source
    assert "预测不展示" in source
    assert "覆盖等级：中 · 仅作研究对照" in source
    assert "predictionDetailBlock" in source
    assert "predictionEvidenceBlock" in source
    assert "</div>${freezeTimeline(prediction)}${predictionEvidenceBlock(prediction)}`" in source
    assert "const numeric = (value) =>" in source
    assert "Number.isFinite(parsed)" in source
    assert "const ladderRows = rows.filter" in source
    assert "暂无可用的三分类评估数据" in source
    assert "无可用市场基线" in source
    assert "live_feature_sources" in source
    execution_predicate = 'const isExecutionLayer = (row) => row?.role === "fetch_runtime" || row?.fact_source === false;'
    assert source.count(execution_predicate) == 1
    assert source.index(execution_predicate) < source.index('const registrySummary = $("#source-registry-summary");')
    assert "factor_trace" in source
    assert "证据与模型步骤" in source
    assert "模型步骤与入模状态" in source
    assert "scoreline_top5" in source
    assert "total_goals_probability" in source
    assert "half_full_probability" in source
    assert "handicap_probability" in source
    assert "total_over_under_probability" in source
    assert "research_predictions" in source
    assert "研究草稿" in source
    assert "match-drawer" in source
    assert "openMatchDrawer" in source
    assert "drawerSourceMarkup" in source
    assert "liveEvidenceForMatch" in source
    assert "current_data?.match_center" in source
    assert "return { events, matchStats: stats, source }" in source
    assert "drawerLiveEvidenceMarkup" in source
    assert "赛中事件与统计" in source
    assert "不回填任何赛前冻结特征或预测" in source
    assert "fixture_evidence" in source
    assert "打开单场分析" in source
    assert "const gateOpen = strictProductionAllowed && prospectivePassed;" in source
    assert "前瞻待评分" in source
    assert '$("#prospective-freeze-status")' in source
    assert "不会改变已锁定模型" in source
    html = HTML.read_text(encoding="utf-8")
    assert 'id="prospective-freeze-status"' in html
    assert 'id="match-drawer"' in html
    assert 'id="match-drawer-content"' in html
    assert "下一冻结机会" in html
    app_server = APP_SERVER.read_text(encoding="utf-8")
    assert "/api/v1/research-predictions" in app_server
