# Matchline 多联赛架构与验收门禁

## 边界

当前迁移采用新增 `league_platform/` 的方式保护既有生产数据：

```text
soccerdata/                 上游数据读取与缓存，不改变
wc_analysis/                旧世界杯生产路径，不覆盖现有 JSON/网页
league_platform/
  catalog.py                七联赛稳定身份与 provider key
  domain.py                 Match、Score、Probability、SourceResult 契约
  sources/match_history.py  football-data.co.uk 历史缓存适配器
  snapshot.py               可复现只读快照
  store.py                  筛选和健康查询
  app.py                    versioned JSON API + 静态 UI
  site/                     全新比赛中心
```

旧 `wc_analysis/data/`、`prediction_history.json` 和生成页面不会被新平台读写。

## 当前契约

### League

```text
id, name_zh, name_en, country_zh, timezone,
match_history_code, football_data_code
```

### Match

```text
id, competition_id, season, kickoff_at,
home_team, away_team, home_team_id, away_team_id, status, score,
market_probability, source{name,file,sha256,provider_fixture_id,retrieved_at,license_status}
```

当前历史文件已有 canonical team ID、provider fixture ID 与 raw SHA-256。由于旧缓存没有可证明的
采集时点，`retrieved_at` 明确为 `null`；实时 provider 接入后才允许填入经过记录的 observed_at，
并保持现有字段向后兼容。

### Prediction

```text
prediction_id, fixture_id, as_of, generated_at,
model_version, feature_version, training_cutoff,
prior, calibrated, score_distribution,
market_reference, data_quality, explanations
```

当前保留旧版 `dynamic_elo_three_way_v1` / `online_dixon_coles_v1` 兼容基线，并以
`strict_dynamic_elo_scoreline_v2_causal_rho` 作为扩展严格报告模型：首批赛季用于参数预热，
之后所有比赛按真实开赛时间 walk-forward 留出。同一开球时刻的比赛先统一预测，再批量更新；
rho 只从当时之前的观察中选择。严格报告按四个连续窗口、目标类型和同市场子样本分别评估，
`/api/v1/model-evaluations` 与 `/api/v1/snapshot` 只在历史截止时间匹配时展示严格附件。

`/api/v1/model-evaluations` 披露严格结果；`/api/v1/predictions` 只处理 `kickoff_at > as_of` 的
当前赛程，明确披露冻结阶段、来源覆盖和 research-only 状态，不把历史回测冒充未来预测。

| 联赛 | 严格留出样本 | 市场同样本 | 模型 Brier | 市场 Brier |
|---|---:|---:|---:|---:|
| 英超 | 3,420 | 2,660 | 0.585388 | 0.571681 |
| 西甲 | 3,420 | 2,660 | 0.590080 | 0.576355 |
| 德甲 | 2,754 | 2,142 | 0.594704 | 0.578663 |
| 意甲 | 3,420 | 2,660 | 0.583441 | 0.571956 |
| 法甲 | 3,097 | 2,337 | 0.600210 | 0.587245 |
| 中超 | 1,101 | 无 | 无公开市场对照 | 无公开市场对照 |

五个有市场基线的联赛中，自建严格模型仍未优于市场，因此只能标记为研究辅助，不能开放生产预测。

## 已验证的数据质量

本地五大联赛缓存单位是一场完赛比赛：

| 联赛 | 行数 | 时间范围 | 重复比赛 | 比分完整率 | 平均 1X2 赔率完整率 |
|---|---:|---|---:|---:|---:|
| 英超 | 4,180 | 2015-08-08 至 2026-05-24 | 0% | 100% | 63.64% |
| 西甲 | 4,180 | 2015-08-22 至 2026-05-24 | 0% | 100% | 63.64% |
| 德甲 | 3,366 | 2015-08-14 至 2026-05-16 | 0% | 100% | 63.64% |
| 意甲 | 4,180 | 2015-08-22 至 2026-05-24 | 0% | 100% | 63.64% |
| 法甲 | 3,857 | 2015-08-07 至 2026-05-17 | 0% | 100% | 63.64% |
| 中超 | 1,581 | 2018-03-03 至 2024-11-02 | 0% | 100% | 0% |

历史账本继续只用于历史分析与回测，不能冒充当前数据。当前正式赛程层只把通过 v260 rights admission
的 OpenFootball CC0 行交给 canonical/current/model 链；中足联官网公开前端 API 仍保留 provider identity，
但因商业复用权未验证只进入 quarantine/阻断诊断，不作为当前 canonical、训练或公开再分发源。中超历史仍由
OpenFootball CC0 独立 adapter 解析，不复用欧洲赔率字段。正式预测仍从 T−24 开始冻结，未到冻结点的远期比赛
只显示赛程与覆盖状态。

## 平台 KPI

### 主要 KPI

1. **可验证赛程覆盖率**：目标联赛中拥有权威赛程/结果源的联赛数 ÷ 6。
2. **合格模型覆盖率**：通过按联赛 walk-forward 门禁的模型数 ÷ 6。
3. **概率校准质量**：每联赛的多分类 Brier、Log loss 和 ECE，相对动态 Elo baseline 改善。

### 驱动指标

- 来源新鲜度达标率；
- fixture canonical ID 匹配率；
- 比分、赔率、xG、阵容的字段覆盖率；
- 可用于严格 OOF 回测的比赛样本量。

### 守护指标

- 未来数据泄漏数必须为 0；
- 重复 fixture 比率必须为 0；
- UI 不能把陈旧数据标为实时；
- 未验证模型不能展示无窗口、无样本量的“命中率”；
- 中超缺失字段不能用欧洲均值静默填充。

## 完成标准

平台最终完成必须同时满足：

- 七联赛当前赛程与结果源均已验证，带来源和采集时间；
- 中超拥有独立身份、参数 profile 和覆盖报告；
- 每个联赛至少通过动态 Elo 与 Dixon-Coles 时间滚动回测；
- 校准器只使用 chronological OOF 预测；
- API 合同测试、数据质量测试和浏览器响应式测试通过；
- 三位独立 agent 对研究、架构、UI、安全和验证证据一致通过；
- 变更以独立分支和可审查提交推送至 GitHub。
