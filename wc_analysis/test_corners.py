#!/usr/bin/env python3
"""
测试角球/定位球能力特征实现
"""
import sys
from pathlib import Path

# 添加 wc_analysis 到 path
sys.path.insert(0, str(Path(__file__).parent))

from predict import get_corner_boost, get_adjustments

def test_corner_boost():
    """测试角球能力加成计算"""
    print("=" * 60)
    print("测试 get_corner_boost()")
    print("=" * 60)

    # 测试有 corners.json 数据的队伍
    test_teams = ["德国", "英格兰", "荷兰", "巴西", "阿根廷"]

    for team in test_teams:
        boost = get_corner_boost(team)
        print(f"{team:8s} → λ加成: {boost:.3f} (λ × {1+boost:.3f})")

    print("\n" + "=" * 60)
    print("测试 fallback Elo 估算(无 corners.json 数据的队伍)")
    print("=" * 60)

    # 测试用 Elo fallback 的队伍
    fallback_teams = ["法国", "西班牙", "葡萄牙", "日本", "突尼斯"]

    for team in fallback_teams:
        boost = get_corner_boost(team)
        print(f"{team:8s} → λ加成: {boost:.3f} (λ × {1+boost:.3f})")


def test_adjustments_integration():
    """测试角球加成是否正确集成到 get_adjustments()"""
    print("\n" + "=" * 60)
    print("测试 get_adjustments() 集成")
    print("=" * 60)

    # 测试对阵: 德国 vs 日本
    home, away = "德国", "日本"
    adj_h, adj_a, notes = get_adjustments(home, away)

    print(f"\n对阵: {home} vs {away}")
    print(f"主队调整因子: {adj_h:.3f}")
    print(f"客队调整因子: {adj_a:.3f}")
    print(f"调整说明:")
    for note in notes:
        print(f"  - {note}")

    # 测试对阵: 英格兰 vs 阿根廷
    home, away = "英格兰", "阿根廷"
    adj_h, adj_a, notes = get_adjustments(home, away)

    print(f"\n对阵: {home} vs {away}")
    print(f"主队调整因子: {adj_h:.3f}")
    print(f"客队调整因子: {adj_a:.3f}")
    print(f"调整说明:")
    for note in notes:
        print(f"  - {note}")


def test_corner_data_format():
    """验证 corners.json 格式"""
    import json
    from predict import CORNERS_FILE

    print("\n" + "=" * 60)
    print("验证 corners.json 格式")
    print("=" * 60)

    if not CORNERS_FILE.exists():
        print(f"⚠️  {CORNERS_FILE} 不存在")
        return

    try:
        data = json.loads(CORNERS_FILE.read_text(encoding="utf-8"))
        print(f"✅ 文件解析成功,包含 {len(data)} 支队伍数据")

        required_fields = ["corners_per_game", "corners_to_goals"]
        for team, entry in data.items():
            if team.startswith("_"):
                continue
            for field in required_fields:
                if field not in entry:
                    print(f"⚠️  {team} 缺少字段: {field}")
                    return

        print("✅ 所有队伍数据格式正确")
        print(f"\n队伍列表: {', '.join([k for k in data.keys() if not k.startswith('_')])}")

    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败: {e}")
    except Exception as e:
        print(f"❌ 验证失败: {e}")


if __name__ == "__main__":
    test_corner_data_format()
    print()
    test_corner_boost()
    print()
    test_adjustments_integration()
    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)
