# Matchline 多联赛架构与验收门禁

## 边界

当前迁移采用新增 `league_platform/` 的方式保护既有生产数据：

```text
soccerdata/                 上游数据读取与缓存，不改变
wc_analysis/                旧世界杯生产路径，不覆盖现有 JSON/网页
league_platform/
  catalog.py                六联赛稳定身份与 provider key
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

当前已实现 `dynamic_elo_three_way_v1` 和 `online_dixon_coles_v1` 两条历史基线：首赛季用于
参数预热，中间赛季用于 rho、候选模型和概率混合校准，末赛季作为完整留出评估。同一开球
时刻的比赛先统一预测，再批量更新。候选选择只看校准赛季，不看留出标签。留出赛季另切成四个
连续审计窗口，并与历史频率、可用时的去水市场概率比较。

`/api/v1/model-evaluations` 披露完整结果；`/api/v1/predictions` 在没有当前赛程输入时明确返回
unavailable，不把历史回测冒充未来预测。实时 Prediction 契约仍属下一阶段。

| 联赛 | 留出样本 | Elo Brier | Dixon-Coles Brier | 市场 Brier |
|---|---:|---:|---:|---:|
| 英超 | 380 | 0.569576 | 0.560969 | 0.533116 |
| 西甲 | 380 | 0.587581 | 0.579204 | 0.564199 |
| 德甲 | 306 | 0.601732 | 0.580345 | 0.559999 |
| 意甲 | 380 | 0.604397 | 0.585364 | 0.577105 |
| 法甲 | 306 | 0.623505 | 0.629303 | 0.609381 |
| 中超 | 240 | 0.581098 | 0.557671 | 无历史赔率 |

五大联赛两条自建模型仍未稳定优于市场基线，因此只能标记为研究基线，不能开放生产预测。

## 已验证的数据质量

本地五大联赛缓存单位是一场完赛比赛：

| 联赛 | 行数 | 时间范围 | 重复比赛 | 比分完整率 | 平均 1X2 赔率完整率 |
|---|---:|---|---:|---:|---:|
| 英超 | 1,140 | 2021-08-13 至 2024-05-19 | 0% | 100% | 100% |
| 西甲 | 1,140 | 2021-08-13 至 2024-05-26 | 0% | 100% | 100% |
| 德甲 | 918 | 2021-08-13 至 2024-05-18 | 0% | 100% | 100% |
| 意甲 | 1,140 | 2021-08-21 至 2024-06-02 | 0% | 100% | 99.91% |
| 法甲 | 1,066 | 2021-08-06 至 2024-05-19 | 0% | 100% | 100% |
| 中超 | 786 | 2022-06-03 至 2024-11-02 | 0% | 100% | 0% |

截至 2026-08-10，这六个联赛的数据全部超过实时新鲜度阈值，因此 API 和 UI 标记为 `stale`，
只能用于历史浏览、回测和开发。中超由 OpenFootball CC0 独立 adapter 解析，不复用欧洲联赛
赔率字段或来源身份。

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

- 六联赛当前赛程与结果源均已验证，带来源和采集时间；
- 中超拥有独立身份、参数 profile 和覆盖报告；
- 每个联赛至少通过动态 Elo 与 Dixon-Coles 时间滚动回测；
- 校准器只使用 chronological OOF 预测；
- API 合同测试、数据质量测试和浏览器响应式测试通过；
- 三位独立 agent 对研究、架构、UI、安全和验证证据一致通过；
- 变更以独立分支和可审查提交推送至 GitHub。
