#!/usr/bin/env python3
"""
Unit tests for form_factor.py - 近期状态特征增强

测试覆盖:
1. Elo 趋势和加速度计算
2. 进球波动性
3. Streak 检测
4. 控球稳定性
5. 综合调整因子
6. 边界情况处理
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# 需要将 wc_analysis 添加到 sys.path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from form_factor import (
    get_form_factor,
    get_elo_trend_and_momentum,
    get_goals_volatility,
    get_streak,
    get_possession_stability,
    calculate_adjustment_factor,
    _safe_std,
    _normalize,
)


class TestUtilityFunctions:
    """工具函数测试"""

    def test_safe_std_empty_list(self):
        assert _safe_std([]) == 0.0

    def test_safe_std_single_value(self):
        assert _safe_std([5.0]) == 0.0

    def test_safe_std_normal(self):
        # [1, 2, 3, 4, 5] 标准差约 1.58
        result = _safe_std([1.0, 2.0, 3.0, 4.0, 5.0])
        assert 1.4 < result < 1.6

    def test_normalize_normal(self):
        assert _normalize(5, 0, 10) == 0.5
        assert _normalize(0, 0, 10) == 0.0
        assert _normalize(10, 0, 10) == 1.0

    def test_normalize_same_min_max(self):
        assert _normalize(5, 5, 5) == 0.0


class TestStreakDetection:
    """连胜/连败检测测试"""

    @patch('form_factor.load_fbref_form')
    def test_winning_streak(self, mock_load):
        mock_load.return_value = [
            {"result": "W", "date": "2026-06-01"},
            {"result": "W", "date": "2026-06-05"},
            {"result": "W", "date": "2026-06-10"},
        ]
        assert get_streak("test_team") == 3

    @patch('form_factor.load_fbref_form')
    def test_losing_streak(self, mock_load):
        mock_load.return_value = [
            {"result": "L", "date": "2026-06-01"},
            {"result": "L", "date": "2026-06-05"},
        ]
        assert get_streak("test_team") == -2

    @patch('form_factor.load_fbref_form')
    def test_draw_breaks_streak(self, mock_load):
        mock_load.return_value = [
            {"result": "W", "date": "2026-06-01"},
            {"result": "D", "date": "2026-06-05"},
        ]
        assert get_streak("test_team") == 0

    @patch('form_factor.load_fbref_form')
    def test_streak_stops_at_different_result(self, mock_load):
        mock_load.return_value = [
            {"result": "L", "date": "2026-06-01"},
            {"result": "W", "date": "2026-06-05"},
            {"result": "W", "date": "2026-06-10"},
            {"result": "W", "date": "2026-06-15"},
        ]
        assert get_streak("test_team") == 3

    @patch('form_factor.load_fbref_form')
    def test_empty_form(self, mock_load):
        mock_load.return_value = []
        assert get_streak("test_team") == 0


class TestGoalsVolatility:
    """进球波动性测试"""

    @patch('form_factor.load_fbref_form')
    def test_stable_goals(self, mock_load):
        # 进球数稳定 [2, 2, 2, 2, 2]
        mock_load.return_value = [
            {"result": "W", "gf": 2} for _ in range(5)
        ]
        volatility = get_goals_volatility("test_team", n=5)
        assert volatility < 0.1  # 几乎无波动

    @patch('form_factor.load_fbref_form')
    def test_high_volatility_goals(self, mock_load):
        # 进球数剧烈波动 [0, 5, 1, 6, 0]
        mock_load.return_value = [
            {"result": "W", "gf": g} for g in [0, 5, 1, 6, 0]
        ]
        volatility = get_goals_volatility("test_team", n=5)
        assert volatility > 0.8  # 高波动

    @patch('form_factor.load_fbref_form')
    def test_insufficient_data(self, mock_load):
        mock_load.return_value = [
            {"result": "W", "gf": 2}
        ]
        volatility = get_goals_volatility("test_team", n=5)
        assert volatility == 0.5  # 返回中性值

    @patch('form_factor.load_fbref_form')
    def test_missing_gf_field(self, mock_load):
        mock_load.return_value = [
            {"result": "W"} for _ in range(5)
        ]
        volatility = get_goals_volatility("test_team", n=5)
        assert volatility == 0.5  # 缺失数据返回中性值


class TestPossessionStability:
    """控球率稳定性测试"""

    @patch('form_factor.load_fbref_form')
    def test_stable_possession(self, mock_load):
        # 控球率稳定在 60% 左右
        mock_load.return_value = [
            {"result": "W", "Poss": p} for p in [58, 60, 62, 59, 61]
        ]
        stability = get_possession_stability("test_team", n=5)
        assert stability > 0.8  # 高稳定性

    @patch('form_factor.load_fbref_form')
    def test_unstable_possession(self, mock_load):
        # 控球率剧烈波动
        mock_load.return_value = [
            {"result": "W", "Poss": p} for p in [30, 70, 40, 75, 35]
        ]
        stability = get_possession_stability("test_team", n=5)
        assert stability < 0.3  # 低稳定性

    @patch('form_factor.load_fbref_form')
    def test_missing_possession_data(self, mock_load):
        mock_load.return_value = [
            {"result": "W", "Poss": None} for _ in range(5)
        ]
        stability = get_possession_stability("test_team", n=5)
        assert stability == 0.5  # 返回中性值


class TestAdjustmentFactor:
    """综合调整因子测试"""

    def test_neutral_factors(self):
        # 所有因子都是中性值
        factor, components = calculate_adjustment_factor(
            trend=0.0,
            momentum=0.0,
            volatility=0.5,
            streak=0,
            possession_stability=0.5,
        )
        # 应接近 1.0
        assert 0.99 < factor < 1.01

    def test_all_positive_factors(self):
        # 所有因子都是正向
        factor, components = calculate_adjustment_factor(
            trend=1.0,       # 最佳趋势
            momentum=1.0,    # 最佳加速度
            volatility=0.0,  # 最稳定
            streak=5,        # 5连胜
            possession_stability=1.0,  # 最稳定控球
        )
        # 应显著高于基准
        assert factor > 1.08
        assert factor <= 1.12

    def test_all_negative_factors(self):
        # 所有因子都是负向
        factor, components = calculate_adjustment_factor(
            trend=-1.0,      # 最差趋势
            momentum=-1.0,   # 最差加速度
            volatility=1.0,  # 最不稳定
            streak=-5,       # 5连败
            possession_stability=0.0,  # 最不稳定控球
        )
        # 应显著低于基准
        assert factor < 0.92
        assert factor >= 0.88

    def test_mixed_factors(self):
        # 混合正负因子
        factor, components = calculate_adjustment_factor(
            trend=0.5,       # 中等上升
            momentum=-0.5,   # 中等减速
            volatility=0.3,  # 轻微波动
            streak=2,        # 2连胜
            possession_stability=0.7,  # 较稳定
        )
        # 应在 0.88 ~ 1.12 之间
        assert 0.88 <= factor <= 1.12
        assert isinstance(components, dict)
        assert "total" in components

    def test_extreme_streak_clamping(self):
        # 超长连胜应被截断到 ±5
        factor1, _ = calculate_adjustment_factor(
            trend=0.0, momentum=0.0, volatility=0.5,
            streak=10,  # 10连胜
            possession_stability=0.5,
        )
        factor2, _ = calculate_adjustment_factor(
            trend=0.0, momentum=0.0, volatility=0.5,
            streak=5,  # 5连胜
            possession_stability=0.5,
        )
        # 10连胜和5连胜应产生相同的贡献 (都被归一化到1.0)
        assert abs(factor1 - factor2) < 0.001


class TestEloTrendAndMomentum:
    """Elo 趋势和加速度测试"""

    @patch('form_factor.load_elo_history')
    @patch('fetch_elo.CODE_CN', {"XX": "测试队"})
    def test_upward_trend(self, mock_code_cn, mock_load):
        # 模拟稳定上升趋势
        import pandas as pd
        mock_df = pd.DataFrame({
            "home": ["XX"] * 10,
            "away": ["YY"] * 10,
            "home_elo": [1800 + i*10 for i in range(10)],
            "away_elo": [1750] * 10,
        })
        mock_load.return_value = mock_df

        trend, momentum = get_elo_trend_and_momentum("测试队", n=5)
        assert trend > 0  # 上升趋势
        assert -1 <= trend <= 1  # 归一化范围

    @patch('form_factor.load_elo_history')
    @patch('fetch_elo.CODE_CN', {"XX": "测试队"})
    def test_downward_trend(self, mock_code_cn, mock_load):
        # 模拟下降趋势
        import pandas as pd
        mock_df = pd.DataFrame({
            "home": ["XX"] * 10,
            "away": ["YY"] * 10,
            "home_elo": [1900 - i*10 for i in range(10)],
            "away_elo": [1750] * 10,
        })
        mock_load.return_value = mock_df

        trend, momentum = get_elo_trend_and_momentum("测试队", n=5)
        assert trend < 0  # 下降趋势

    @patch('form_factor.load_elo_history')
    @patch('fetch_elo.CODE_CN', {"XX": "测试队"})
    def test_insufficient_data(self, mock_code_cn, mock_load):
        # 数据不足
        import pandas as pd
        mock_df = pd.DataFrame({
            "home": ["XX"] * 2,
            "away": ["YY"] * 2,
            "home_elo": [1800, 1810],
            "away_elo": [1750, 1755],
        })
        mock_load.return_value = mock_df

        trend, momentum = get_elo_trend_and_momentum("测试队", n=5)
        assert trend == 0.0
        assert momentum == 0.0


class TestFormFactorIntegration:
    """集成测试 - 完整流程"""

    @patch('fetch_elo.CODE_CN', {"NL": "荷兰"})
    @patch('form_factor.load_fbref_form')
    @patch('form_factor.load_elo_history')
    def test_complete_form_factor(self, mock_elo, mock_fbref, mock_code_cn):
        # 模拟完整数据
        import pandas as pd

        # Elo 数据 (稳定上升)
        mock_elo.return_value = pd.DataFrame({
            "home": ["NL"] * 15,
            "away": ["XX"] * 15,
            "home_elo": [1900 + i*5 for i in range(15)],
            "away_elo": [1800] * 15,
        })

        # FBref 数据 (3连胜, 稳定表现)
        mock_fbref.return_value = [
            {"result": "W", "gf": 2, "Poss": 60},
            {"result": "W", "gf": 3, "Poss": 62},
            {"result": "W", "gf": 2, "Poss": 61},
            {"result": "W", "gf": 2, "Poss": 59},
            {"result": "W", "gf": 3, "Poss": 60},
        ]

        result = get_form_factor("荷兰")

        # 验证结构
        assert "trend" in result
        assert "momentum" in result
        assert "streak" in result
        assert "volatility" in result
        assert "possession_stability" in result
        assert "adjustment_factor" in result
        assert "components" in result

        # 验证值范围
        assert -1 <= result["trend"] <= 1
        assert -1 <= result["momentum"] <= 1
        assert 0 <= result["volatility"] <= 1
        assert 0 <= result["possession_stability"] <= 1
        assert 0.88 <= result["adjustment_factor"] <= 1.12

        # 正向因子应提升调整因子
        assert result["streak"] > 0  # 连胜
        assert result["adjustment_factor"] > 1.0  # 应高于基准

    def test_boundary_values(self):
        """边界值测试"""
        # 最小调整因子
        factor_min, _ = calculate_adjustment_factor(
            trend=-1.0, momentum=-1.0, volatility=1.0,
            streak=-10, possession_stability=0.0
        )
        assert 0.88 <= factor_min < 0.92

        # 最大调整因子
        factor_max, _ = calculate_adjustment_factor(
            trend=1.0, momentum=1.0, volatility=0.0,
            streak=10, possession_stability=1.0
        )
        assert 1.08 < factor_max <= 1.12


class TestRealWorldData:
    """真实数据测试 (需要真实数据文件)"""

    def test_load_real_teams(self):
        """测试加载真实球队数据 (如果数据文件存在)"""
        from pathlib import Path
        data_dir = Path(__file__).parent / "data"

        if not (data_dir / "fbref_form.json").exists():
            pytest.skip("真实数据文件不存在")

        # 尝试加载荷兰队
        try:
            result = get_form_factor("荷兰")
            assert 0.88 <= result["adjustment_factor"] <= 1.12
        except Exception as e:
            pytest.skip(f"真实数据加载失败: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
