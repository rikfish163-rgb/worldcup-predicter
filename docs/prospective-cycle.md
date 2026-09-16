# Matchline 前瞻周期主线

这份文档只描述当前可运行的前瞻链，不记录已经淘汰的 v22–v294 迭代日志；旧日志和临时验收文件已按
[`docs/evidence/cleanup-v293-mainline-clean-2026-08-26.json`](evidence/cleanup-v293-mainline-clean-2026-08-26.json)
及其增量 [`docs/evidence/cleanup-v294-mainline-prune-2026-08-26.json`](evidence/cleanup-v294-mainline-prune-2026-08-26.json)
的归档边界移到外置盘。

## 当前链路

```text
OpenFootball / 允许的公开事实源
        │  (时间、权利、raw archive、实体一致性门)
        ▼
sync_live → current.json + source archive
        │
        ├─ capture_prospective → prospective_predictions.jsonl
        ├─ evaluate            → prospective-evaluation-current.json
        ├─ strict_report       → strict-backtest-current.json
        └─ build_offline_bundle → Sites offline_snapshot.json
                                      │
                                      ▼
                              maturity / publication gates
```

当前 runtime-only 服务使用：

- runtime：`/dev/shm/matchline-live-runtime`
- OpenFootball 原始归档：`/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw`
- 模型锁：`docs/evidence/prospective-model-lock-current.json`
- 周期：`matchline-prospective-cycle-runtime-only.timer`，每 15 分钟
- 读模型：`matchline-runtime-readmodel.timer`，只从同一 runtime 证据构建 Sites 快照

默认输出路径仍保留给本地测试和兼容 CLI；生产 unit 必须显式传入 runtime 与 durable raw archive，
不能把旧 `data/live` 快照当作当前数据。

Wikidata 场馆坐标是可选的展示增强，不是周期主链依赖。正式 prospective systemd lane 显式追加
`--enable-wikidata`，以便在比赛工作台展示可验证的场馆上下文；其失败只应影响场馆上下文，不改变主预测链的时间边界。
适配器默认每轮最多发起 16 次 Wikidata 请求（操作员可在调用层提高到 96 次上限）；达到预算后返回
`wikidata_request_budget_exhausted`，保留已成功的有限结果并结束该来源，不得让实体补充把主周期变成无界轮询。

## 冻结与评估

每场比赛按真实时间顺序分别冻结 `t_minus_24h`、`t_minus_6h`、`t_minus_90m` 和确认首发阶段。
阶段只允许使用 cutoff 之前已经观察到的事实；同一开球批次先统一生成预测，再批量写入赛果状态。
预测保存胜平负、让球、总进球、比分矩阵和半全场，但低覆盖字段保持缺失，不补零。

评估只读取开赛后实际观察到的终场结果。缺少可信 raw replay、未来时间戳、来源权利或实体唯一联结时，
该行进入 quarantine，不计入 `scored_n`、样本门槛或模型晋级。

## 当前真实状态

最近一次 runtime-only 周期的快照、周期和评估回执写在 runtime 根目录，而不是覆盖仓库内的 dated
evidence。当前状态仍是 `pending_prospective_window` / `scored_n=0` / `promotion_eligible=false`；
这表示前瞻样本尚未完成，不表示模型已经收敛或具备投注优势。

Sites 公开包必须同时通过来源权利、raw provenance、快照一致性、模型锁、maturity 和 publication
审计。任何一项失败都会保留上一份可验证资产或写明确的空/阻断状态，不把旧数据伪装成 fresh。

## 常用检查

```bash
systemctl --user status matchline-prospective-cycle-runtime-only.service
systemctl --user status matchline-runtime-readmodel.service
systemctl --user list-timers 'matchline-*'

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  tests/test_prospective_cycle.py tests/test_prospective_evaluation.py \
  tests/test_openfootball_raw_archive.py
```

发布前还必须运行完整的 maturity/platform verification；`production_allowed=false` 或任一 raw/lock
绑定缺失时，禁止 D1、会员或公网发布。
