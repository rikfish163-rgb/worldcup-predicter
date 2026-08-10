# 当前数据与未来预测契约

## 不可混淆的两层

1. `data/MatchHistory` 中的 football-data.co.uk 与 OpenFootball 数据只用于训练、校准和回测。
2. `data/live/current.json` 只保存带 `as_of/retrieved_at/raw_sha256` 的当前来源快照，用于确认未来
   fixture 和构造预测时点特征。

历史比赛不会因为仍在本地而被标成“当前”，当前快照超过 6 小时后预测 API 自动变为 unavailable。

## 多源职责

| 信息角色 | 当前实现 | 覆盖 | 门禁 |
|---|---|---|---|
| 当前赛程与结果 | ESPN scoreboard | 五大联赛 + 中超 | 原生 fixture/team ID、45 天窗口、6 小时新鲜度 |
| 最近 xG 与状态 | Understat | 五大联赛 | 只取 `as_of` 前已完赛数据，最近 5 场；中超保持缺失 |
| 历史训练比分/赔率 | football-data.co.uk | 五大联赛 | 固定 URL + SHA-256；永不冒充当前数据 |
| 中超历史训练 | OpenFootball CC0 | 2022–2024 | 独立 parser/身份/参数；无赔率保持空值 |
| 当前市场赔率 | 尚未接入 | 0/6 | 所有预测标记 research-only |
| 伤停与确认首发 | 尚未接入 | 0/6 | 所有预测标记 research-only |

同步命令：

```bash
.venv/bin/python -m league_platform.sync_live
```

2026-08-10 的真实联网验收结果：ESPN 返回 283 场未来赛程，其中英超 50、西甲 70、德甲 36、
意甲 50、法甲 45、中超 32；Understat 返回 96 支五大联赛球队的最近 xG/状态，两源错误数均为 0。

## 未来预测契约

`GET /api/v1/predictions` 只处理满足 `kickoff_at > as_of` 的比赛，并返回：

- ESPN 原生 fixture ID 与开赛时间；
- `as_of` 和历史 `training_cutoff`；
- Elo 与 Dixon-Coles 两条概率，不隐藏候选差异；
- 参与该场的 provider 列表和当前特征覆盖；
- 对升班马/新球队的历史样本不足阻断；
- 当前赔率、伤停、首发缺失门禁。

当前输出属于 `research_only`。即使模型能计算概率，只要当前市场和阵容源未通过新鲜度与覆盖门禁，
就不能标记 production-ready，也不能形成投注金额或收益建议。
