# 严格赛前回测主线

严格报告是研究和发布门禁的输入，不是投注建议。历史候选、参数搜索和逐轮报告已经归档；本文件只
保留当前协议和入口。

## 协议

- 按比赛真实开球时间 walk-forward；同一开球时间先预测、后批量更新状态。
- 每个特征必须满足 `observed_at/effective_at <= cutoff_at`，无法证明时钟就隔离。
- 训练、校准、留出和前瞻评分严格分开；赛后事件、直播状态、未来首发不能回填赛前阶段。
- 输出覆盖胜平负、让球、总进球、精确比分矩阵和半全场；每个目标独立统计样本和校准。
- 市场基线只在同一场、同一时间口径和完整字段子样本上比较；缺失市场不补零。
- 未经独立 raw archive 重放、来源权利或模型锁校验的行不进入 formal report。

## 当前输入

- 正式历史来源：经 v260 rights admission 的 OpenFootball raw archive；来源、配置、内容哈希和确定性
  fixture ID 必须现场重验。
- 德甲 OpenLigaDB 只作有归属的 display/post-match 交叉核验，不进入训练或再分发。
- football-data、旧 ESPN、OddStorm、未知权利 Crawl4AI 结果只可作为隔离审计，不能被 current、formal
  report 或 publication 复活。
- 模型锁：`docs/evidence/prospective-model-lock-current.json`；锁定后不得修改模型文件，
  如需变更必须生成新锁和新的 evaluation window。

## 当前报告与运行入口

历史形式化 v260 报告已移入外置归档；实际定时任务把当前报告原子写入
`${MATCHLINE_RUNTIME_DIR}/strict-backtest-current.json`，并用内容 SHA-256 保存到 durable
runtime archive。旧 dated 文件不作为当前指针。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m league_platform.strict_report \
  --openfootball-raw-archive /media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw \
  --observed-before 2026-08-25T18:07:08Z \
  --prospective-lock docs/evidence/prospective-model-lock-current.json
```

缺少 durable raw archive、路径位于 `/dev/shm`、锁完整性失败、来源权利未决或历史语料为空时，CLI
必须 fail-closed；不得以旧报告或自报哈希补齐。

## 发布解释

当前报告和前瞻评估仍属于 `research_only`。只有每联赛样本门槛、每个玩法独立评分、校准、市场对照、
来源可追溯性、Sites/D1 一致性和 maturity 全部通过，才可能进入商品化审查；任何“模型不劣于历史频率”
的结果也不等于击败市场或保证收益。
