# Matchline 当前主线状态

> 2026-09-07 核查提示：下方正文是 2026-08-26 的历史状态，不能作为当前运行或上线结论。当前工程事实、未上线补丁、差距与执行顺序见 [工程现状与计划](engineering-status-and-plan-2026-09-07.md)。保留旧正文用于历史追溯。

更新时间：2026-08-26 22:40（Asia/Shanghai）

这份文件只保留当前主线结论。逐轮收据、旧候选锁和历史严格报告已经移到外置归档，不作为当前运行输入。

## 当前运行链

- 活动前瞻锁：`docs/evidence/prospective-model-lock-current.json`
- 锁状态：`pending_prospective_window`，52 个模型文件中有 8 个现场哈希不匹配；不刷新旧锁掩盖漂移。
- 最近活动 runtime-only 周期：2026-08-26 22:37:46 因旧锁的 `league_platform/capture_prospective.py` 锁哈希不匹配退出码 1；同步仍得到 844 场 OpenFootball 赛程，但冻结、评估和发布均未执行；活动锁没有被刷新或替换。
- 当前周期正式预测、待评分和已评分仍为 0，`promotion_eligible=false`；没有 D1 写入或公网发布。
- v294 候选锁在外置隔离目录中生成时 52/52 一致，聚合哈希也已重算一致，未替换活动锁；后续任何模型输入改动都必须重新验证，不能把候选锁当作当前锁。独立 tmpfs 演练成功同步 844 场 OpenFootball 赛程、未来 7 日 71 场，回放 69 场历史比赛；因当前 5 个联赛尚未到达 T−24H 截止点且 1 场早于新窗口，正式预测为 0，`promotion_eligible=false`。演练没有 D1 或公网写入，候选文件位于外置隔离归档，不进入当前运行链。
- 2026-08-26 22:13 UTC 的候选锁 canary 在外置隔离目录中再次完整跑通：`sync=844`、未来 7 日 `71`、可信历史回放 `69`，OddStorm 因权利门为 `rights_blocked` 且未联网；因果窗口尚未产生可冻结的 T−24H 比赛，`predictions=0`、`promotion_eligible=false`。周期回执 SHA-256 为 `7f6c99f26d268d949ff603584118650eadf87dd84fdb9f07e8cce040b302cfe2`，评估回执 SHA-256 为 `3082adb39bfe8afd8b1bf59e9493cd78f51b024aa0d57857be543d0a85017ec2`，原始回执保存在 `/media/hetaisheng/044A81D94A81C83E/soccerdata-iteration-archive-2026-08-26/canaries/v294-candidate-cycle-20260826-2215-r1/`，不进入当前运行链。
- Wikidata 只有在正式周期成功运行后才会按 16 次预算执行；当前活动周期被锁门阻断，现场状态仍是 `not_configured`，不能把候选探针结果当作当前覆盖。

## Sites 主线

`matchline-runtime-readmodel.service` 最近一次成功构建于 2026-08-26 22:19。当前离线快照为
`asOf=2026-08-26T14:06:44.995785+00:00`、`generatedAt=2026-08-26T14:19:06.084850+00:00`，
`build_id=bundle-b366a3a920271515d467`；包含 369 条合并赛程、0 条正式预测和 71 条研究基线，
`productionReady=false`、`live=false`，publication audit 为 `blocked`。

当前赛程总量为 844 场，但离线证据包不是 D1 连接；页面指标必须显示“可查询赛程”，不能显示“D1 比赛”。
研究基线只用于研究和回测，低覆盖字段保持缺失，不补零、不伪造完整情报。

当前未来 7 日窗口可查询 71 场研究记录，其中 59 场通过同一份 verified OpenFootball 历史先验合同，可展示历史 1X2、历史 xG、Top 比分和训练截止时间。它们仍全部标为低覆盖研究数据；伤停、首发、天气和市场缺失时不展示赛前概率，也不构成投注优势。比赛列表和研究队列共用严格投影，伪造或混源 baseline 会显示为“未生成”。

live overlay 最近一次运行返回 `rights_blocked`，`fixture_count=0` 且 `network_opened=false`；未经核验的实时来源没有联网抓取或写入实时行。

## 发布门

只有以下条件同时满足，才允许进入会员或公网发布：

1. 前瞻窗口产生足够的锁后留出比赛；
2. 胜平负、进球、比分和半全场分别达到独立样本门槛；
3. 严格报告、市场对照和校准证据绑定同一模型锁；
4. 所有公开字段有可验证来源、时间和权利合同；
5. Sites、D1、模型和发布回执哈希一致。

当前系统保持研究模式，不输出保证性投注建议。当前证据不能支持“已击败 SofaScore/市场”或“可以售卖会员”。

## 归档边界

旧迭代账本、候选锁、严格报告、截图、缓存和运行前 unit 备份位于：

`/media/hetaisheng/044A81D94A81C83E/soccerdata-iteration-archive-2026-08-26`

清理汇总见 `docs/evidence/cleanup-v293-mainline-clean-2026-08-26.json`，淘汰证据与生成缓存的增量见
`docs/evidence/cleanup-v294-mainline-prune-2026-08-26.json`、
`docs/evidence/cleanup-v295-mainline-generated-2026-08-26.json`、
`docs/evidence/cleanup-v296-post-test-logs-2026-08-26.json` 和
`docs/evidence/cleanup-v297-final-audit-tmp-2026-08-26.json`、
`docs/evidence/cleanup-v298-full-test-logs-2026-08-26.json`。更早的逐轮收据已按原 SHA-256 可恢复地
移到外置归档 `cache/cleanup-v293-history-2026-08-26/`，不再混入主线证据目录。
没有执行 Git reset、删除历史数据、D1 写入或公网部署。
