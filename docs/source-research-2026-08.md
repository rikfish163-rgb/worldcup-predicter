# 来源研究主线（2026-08）

这份文档只保留当前来源决策。逐次探针、旧 URL 和历史候选的明细已经随迭代归档；它们不能解除
当前的权利或时间门禁。

## 当前允许进入主线的来源

| 来源 | 允许用途 | 必要条件 |
|---|---|---|
| OpenFootball | 当前赛程/赛果、历史训练、正式前瞻 | 固定 allowlist、CC0 配置精确匹配、durable raw archive、现场重解析、确定性 fixture ID 和 cutoff 通过 |
| Wikidata | 场馆/实体辅助 | 仅保存可验证实体字段和内容哈希，不猜城市或球队 |
| MET Norway | 天气候选 | 署名与字段来源完整；天气默认展示/审计，未通过独立系数验证不入模 |
| OpenLigaDB | 德甲 display/post-match 交叉 | ODbL attribution；不作为 canonical authority、训练输入或公开再分发源 |

## 当前隔离的来源

- ESPN、SofaScore、WhoScored、FBref、ClubElo：公开可达不等于获得商业自动化或再分发许可；默认
  `rights_blocked`，不发起受限网络请求，不回放旧快照。
- OddStorm、体彩赔率和其他盘口页：购买场景与研究市场基线必须分别核验授权、时间语义和结算规则；
  当前未满足时保持 `market_unavailable`，不把旧赔率或自报哈希当作实时市场。
- Crawl4AI 页面：Crawl4AI 只是执行层；无独立事实源身份、许可证和 raw provenance 的页面一律隔离。
- 官方首发/伤停/新闻：只有完整、可核验、开赛前观察时才可能进入对应阶段；情绪和舆情默认只展示，
  不直接改变概率。

## 统一判断

公开网页的 HTTP 200 只能证明一次网络观察，不证明全球可用性、授权、字段真实性或赛前时间语义。
每个适配器必须保存 URL、`effective_at`、`observed_at`、响应 SHA-256、解析状态和实体匹配置信度。
来源冲突保留全部原始记录并降低覆盖；不能解决时禁止进入模型和 publication。

当前权利门、raw admission 和运行状态以：

- `league_platform/source_rights.py`
- `league_platform/openfootball_raw_archive.py`
- `docs/evidence/prospective-model-lock-current.json`
- `/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw`

为准。历史探针只用于审计和恢复，不是当前生产输入。
