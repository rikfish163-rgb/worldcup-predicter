# 当前数据与未来预测契约

## 不可混淆的两层

1. `data/MatchHistory` 中的 football-data.co.uk 与 OpenFootball 数据只用于训练、校准和回测。
2. `data/live/current.json` 只保存带 `as_of/retrieved_at` 和内容哈希的当前来源快照，用于确认未来
   fixture 和构造预测时点特征。每个 HTTP 响应在实际收到后记录 `retrieved_at`，最终 `as_of`
   在全部来源完成后生成；因此所有被采用的观察都满足 `retrieved_at <= as_of`。

历史比赛不会因为仍在本地而被标成“当前”。服务会在每次 snapshot/health/predictions 请求时重新
读取并校验当前快照；超过 6 小时后预测 API 自动变为 unavailable，不依赖进程重启。

## 多源职责

| 信息角色 | 当前实现 | 覆盖 | 门禁 |
|---|---|---|---|
| 当前赛程与结果 | ESPN scoreboard | 五大联赛 + 中超 | 原生 fixture/team ID、近 45 天结果 + 未来 45 天赛程、6 小时新鲜度 |
| 最近 xG 与状态 | Understat | 五大联赛 | 只取 `as_of` 前已完赛数据，最近 5 场；中超保持缺失 |
| 历史训练比分/赔率 | football-data.co.uk | 五大联赛 | 固定 URL + SHA-256；永不冒充当前数据 |
| 中超历史训练 | OpenFootball CC0 | 2022–2024 | 独立 parser/身份/参数；无赔率保持空值 |
| 当前市场赔率 | ESPN event summary / DraftKings | 六联赛均有部分覆盖 | 按 fixture 抓取、记录 bookmaker、去水；无覆盖保持空值 |
| 公开赛前新闻 | BBC Sport / Sky Sports RSS + Google News RSS 发现查询 | 全局/联赛新闻，需按队名与官方来源核验 | 固定 HTTPS allowlist、发布时间与原始哈希；源级失败降级 |
| 比赛天气 | Open-Meteo | 仅有可靠场地坐标的比赛 | 精确开球小时；无坐标不猜测，记录 unavailable |
| 伤停与确认首发 | SofaScore provider-reported | 依赖事件匹配与接口可用性 | `missingPlayers`/首发原样披露；不宣称权威，不伪造无伤停 |

ESPN JSON 保存响应内容 SHA-256；Understat 同时保存压缩传输字节的 `wire_sha256` 与解压内容的
`content_sha256`。任一来源启动失败会记录结构化错误并降级，不会丢弃其他已成功来源。
空赛程、来源错误、联赛覆盖不完整、非 allowlist 主机、非有限数值或不完整比分都会使快照降级、
不可用或直接拒绝，不能仅凭一个新的 `as_of` 冒充 fresh。

同步命令：

```bash
.venv/bin/python -m league_platform.sync_live
```

同步会同时抓取近 45 天 ESPN 完赛结果与未来 45 天赛程，并按源记录 `retrieved_at`、原始哈希、结构化错误；
每次同步还会将完整快照追加到 `data/live/archive/`，原始内容按 SHA-256 去重保存，支持回放赔率/新闻/天气/阵容
在当时截点的覆盖变化。新闻、天气与 SofaScore 属于可选源，短暂不可用不会掩盖核心赛程源状态。

## 未来预测契约

`GET /api/v1/predictions` 只处理满足 `kickoff_at > as_of` 的比赛，并返回：

- ESPN 原生 fixture ID 与开赛时间；
- `as_of` 和历史 `training_cutoff`；
- Elo 与 Dixon-Coles 两条概率，不隐藏候选差异；
- 参与该场的 provider 列表和当前特征覆盖；
- 对升班马/新球队的历史样本不足阻断；
- 当前赔率、伤停、首发缺失门禁。

当前输出属于 `research_only`。每场比赛分别披露市场、天气、新闻和 SofaScore 覆盖；模型只有在
伤停/首发完整时才会把对应字段标为 available，但这些新增特征尚未通过独立留出回测，不能解除生产门禁。
任何情况都不能形成投注金额或收益建议。
