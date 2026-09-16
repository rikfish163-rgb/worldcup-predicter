# Matchline 研究闭环最终状态报告（2026-09-16）

> 对应已批准目标：`.pi/goal/完成-matchline-研究闭环并建立合规数据源抓取能力-20260916-0219.md`。本报告只记录已观察证据；正式概率门禁仍关闭。

## 当前结论

- 来源审计：**满足**，`47` 条 registry/SourceId 并集来源，无未分类 `unknown`。
- 合规 facts 接入：VPS r9 已激活单一 systemd writer；最新 facts 读回为 `2026-09-16T11:57:32.000Z`，OpenFootball `2916` 条、OpenFootball history `8763` 条、OpenLigaDB `2240` 条、Wikidata `4` 个、MET `12` 条；payload 中预测/赔率均为 0；另有 `55` 条允许来源 raw receipts。
- 研究评估：真实前瞻账本 `scored_n=158`、`pending_n=180`，冲突 `0`，指标有效；仍为 research-only。
- 生产门槛：**未满足，继续关闭**：`production_allowed=False`、`promotion_eligible=False`、样本门槛 `False`、冻结验证 `False`。
- 发布：父仓库代码提交 `18c1bfa` 已由 GitHub 非 main ref 确认；最终交接证据随后随该非 main 分支继续提交。Sites 本地 `9d424d0`，远端推送因缺少认证失败；Cloudflare deploy 因缺少 `CLOUDFLARE_API_TOKEN` 未执行。

## 证据索引

| 证据 | 路径 | SHA-256 / 结果 |
|---|---|---|
| 逐源 JSON | `docs/evidence/source-research-2026-09-16-r2.json` | `ea283846a64b3af5b2df3698fcb7645c8a5c8f4d2cca5a36ba40c7a14dc1118c` |
| 逐源 Markdown | `docs/source-research-2026-09-16-r2.md` | `6b8ef0d6d238a1cb2dcc2f7aeb98d3c7d47e9f255033a6568a9d52c478827e52` |
| r2 probes | `docs/evidence/source-probe-evidence-2026-09-16-r2.json` | `a48fbec92745ae8a2434e48eab1e753475f3a2095af94c9765eaea6c7064755c` |
| 本地真实 facts refresh | `docs/evidence/facts-refresh-evidence-2026-09-16.json`、`facts-refresh-projection-2026-09-16.json` | `56157dead0506b9b6e15ec59cead14989ce441c7313570121f79ad6d071e0b2b` / `04fabb78cb553104fa39529227ec7015c1f72e84c5f38f574b5d8926db0bfcc8` |
| VPS r9 release | `docs/evidence/vps-release-r9-final-v2-2026-09-16.json` | `cbf4c299ac8750795c887e16f75dbffb4a3eaba0e0d88061952536c3f9bf6906` |
| VPS facts readback | `docs/evidence/vps-r9-facts-readback-2026-09-16.json` | `576a617e7d29253cbdd5d0c1dae9ca334d16dccee79d55c6a7e42ee81f4c0fc5` |
| VPS raw archive summary | `docs/evidence/vps-r9-raw-archive-summary-2026-09-16.json` | `b4736c0679ad50efc7f15b187113ee92f625f8e76e5b6b1eacf50282f7fd0b68`；55 receipts；4,792,065 bytes |
| VPS source catalog readback | `docs/evidence/vps-r9-source-catalog-readback-2026-09-16.json` | `b449a5c02ed6ab788d8b25431b1e2939206fca6ecc6ae051b29eec038fc0e4c0`；unknown `0` |
| VPS raw archive/restore/rollback | `docs/evidence/vps-r9-archive-restore-rollback-2026-09-16.json` | `8a507ae7228986ae027ce72defb06ceb95835ba987b1f026a09c30833dd9f866`；55 receipts；restore match `True`；upload HTTP `201` |
| 前瞻评估 | `docs/evidence/prospective-evaluation-runtime-only-2026-09-16.json` | `2939bbf5eae7d95c70f8cfd6152b030faf8712ec0f2230eca9a5eec3c3d62b36` |
| 评分审计 | `docs/evidence/prospective-audit-scored-2026-09-16.json` | `83b0518849ea5ab6055978b87aef7f4b72f1980247767887b5711c0cf7248df7` |
| 机器验证摘要 | `docs/evidence/verification-summary-2026-09-16.json` | `e6c35bfd3e03105d5baeb837018ff77217a601465c65843e8d75be19b8811dd0` |

## Acceptance criteria 逐条验收

### 1. 逐源深度研究 — **Met**

- r2 JSON/Markdown 覆盖 `47` 个来源；validator 强制 registry union、rights/HTTP state、字段契约、SHA、时钟和 eligibility，未分类 unknown 会拒绝。
- 4 个允许端点保留真实 probe：OpenFootball、OpenLigaDB、Wikidata、MET Norway；受限来源未执行网络请求。
- 逐源索引如下；完整条款 URL、allowlist、限速、解析、字段、实体、双时钟、失败、替代方案见逐源报告。

| source_id | rights classification | status | HTTP | network | display/model/publication |
|---|---|---|---|---|---|
| bundesliga_public_pages | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| cfl_official_current | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| checkbestodds_public_pages | rights_unverified | not_observed | not_observed | not opened | blocked_pending_rights_review / blocked / blocked |
| clubelo_public_ratings | rights_unverified | quarantined | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| crawl4ai_allowlisted_pages | execution_layer_not_fact_source | rights_blocked | not_attempted | not opened | conditional_per_declared_page / blocked_by_default / blocked_by_default |
| espn_injury_reports | provider_permission_required | rights_blocked | not_attempted | not opened | blocked / blocked / blocked |
| espn_market_summary | provider_permission_required | rights_blocked | not_attempted | not opened | blocked / blocked / blocked |
| espn_schedule_summary | provider_permission_required | rights_blocked | not_attempted | not opened | blocked / blocked / blocked |
| espn_team_rosters | provider_permission_required | rights_blocked | not_attempted | not opened | blocked / blocked / blocked |
| fbref_public_stats | rights_unverified | quarantined | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| five_hundred_league_public_pages | rights_unverified | not_observed | not_observed | not opened | blocked_pending_rights_review / blocked / blocked |
| football_data_china_public_csv | rights_unverified | not_observed | not_observed | not opened | blocked_pending_rights_review / blocked / blocked |
| football_data_historical | public_download_rights_not_stated_for_product_use | training_only | training_only | not opened | blocked_current / blocked_current / blocked_current |
| fotmob_public_api | robots_policy_blocked | blocked_by_robots | not_attempted | not opened | blocked / blocked / blocked |
| laliga_official_match_directory | rights_unverified | unavailable | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| laliga_official_news | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| laliga_public_pages | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| lazq_public_mirror | rights_unverified | quarantined | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| legacy_anti_waf_relay | forbidden_access_control_bypass_artifact | forbidden | not_attempted | not opened | blocked / blocked / blocked |
| ligue1_official_news | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| met_norway_weather | verified_cc_by_forecast_with_identifying_user_agent | degraded | success | opened | allowed_with_attribution / conditional_exact_coordinates_and_time / conditional_with_attribution |
| oddstorm_market_comparison | provider_permission_required | rights_blocked | not_attempted | not opened | blocked / blocked / blocked |
| oddstorm_market_history | provider_permission_required | not_observed | not_observed | not opened | blocked / blocked / blocked |
| official_bundesliga_lineups | first_party_access_reuse_terms_not_verified | not_observed | not_observed | not opened | blocked_pending_terms_review / blocked / blocked |
| official_laliga_lineups | first_party_access_reuse_terms_not_verified | not_observed | not_observed | not opened | blocked_pending_terms_review / blocked / blocked |
| official_league_lineups | first_party_access_reuse_terms_not_verified | unavailable | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| official_ligue1_lineups | first_party_access_reuse_terms_not_verified | not_observed | not_observed | not opened | blocked_pending_terms_review / blocked / blocked |
| official_premier_league_lineups | first_party_access_reuse_terms_not_verified | not_observed | not_observed | not opened | blocked_pending_terms_review / blocked / blocked |
| official_serie_a_lineups | first_party_access_reuse_terms_not_verified | not_observed | not_observed | not opened | blocked_pending_terms_review / blocked / blocked |
| open_meteo | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| open_meteo_geocoding | free_tier_noncommercial_cc_by_only | not_observed | not_observed | not opened | blocked_for_current_commercial_release / blocked_pending_commercial_plan / blocked_pending_commercial_plan |
| open_meteo_weather | free_tier_noncommercial_cc_by_only | not_observed | not_observed | not opened | blocked_for_current_commercial_release / blocked_pending_commercial_plan / blocked_pending_commercial_plan |
| openfootball_current | verified_cc0_public_domain | fresh | success | opened | conditional_after_admission / conditional_after_causal_gate / conditional_after_maturity_gate |
| openfootball_historical | verified_cc0_public_domain | training_only | not_observed | not opened | conditional_after_admission / conditional_after_causal_gate / conditional_after_maturity_gate |
| openligadb_secondary_results | verified_odbl_isolated_display_current | fresh | success | opened | allowed_isolated / blocked / blocked |
| osm_geodata | rights_metadata_present_adapter_contract_pending | future_disabled | not_observed | not opened | blocked_until_adapter_contract / blocked / blocked |
| pappalardo_wyscout_historical | rights_metadata_present_adapter_contract_pending | future_disabled | not_observed | not opened | blocked_until_adapter_contract / blocked / blocked |
| premier_league_public_pages | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| public_rss_news | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| seriea_public_pages | first_party_access_reuse_terms_not_verified | rights_blocked | not_attempted | not opened | blocked_pending_terms_review / blocked / blocked |
| sevenm_csl_public_fixture_script | rights_unverified | not_observed | not_observed | not opened | blocked_pending_rights_review / blocked / blocked |
| sofascore_prematch | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| sofifa_public_reference | robots_policy_blocked | blocked_by_robots | not_attempted | not opened | blocked / blocked / blocked |
| sports_lottery_official | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| understat_xg | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| whoscored_public_pages | rights_unverified | rights_blocked | not_attempted | not opened | blocked_pending_rights_review / blocked / blocked |
| wikidata_entities | verified_cc0_structured_data | degraded | success | opened | allowed_with_attribution_preference / conditional_exact_venue_chain / conditional_with_provenance |

### 2. 可用来源真实接入 — **Met**

- r9 生产 collector 对允许的 OpenFootball 当前/历史、OpenLigaDB 十个 allowlisted 赛事、Wikidata 和 MET Norway 成功响应写入独立 `raw-archive/`；共 `55` 条 receipt、`4,792,065` bytes，逐条 object hash、大小、路径和 manifest 均已验证；按来源汇总见 `vps-r9-raw-archive-summary-2026-09-16.json`。
- 同一批响应被解析成 `2916` 条当前 OpenFootball、`8763` 条历史 OpenFootball、`2240` 条 OpenLigaDB、`4` 个 Wikidata、`12` 条 MET facts，并以 facts-only envelope HTTP 201 发布；线上 readback HTTP 200 的 as-of/hash 与 r9 一致。
- OpenLigaDB 仍是隔离 display/post-match lane，Wikidata/MET 按各自 attribution/model eligibility 投影；22 项 collector/publisher 回归覆盖 raw archive 幂等、允许来源存档和受限来源零网络。

### 3. 受限来源处理 — **Met**

- r9 source catalog unknown 数为 0；10 个受限例（8 个未核权 Football-Data current sidecar、ESPN、SofaScore）均为 `blocked`，`recordCount=null`、无 raw 成功哈希、无网络成功声明。
- ESPN/SofaScore diagnostic 已改为无网络 rights block；未核权 Football-Data current collection 默认不发起 HTTP。受限来源不进入模型、预测或公开 facts rows。

### 4. 真实前瞻闭环 — **Met**

- 当前锁 `357a2d4c1082552ef8515e4fee327e75243f5dca4aa69d0047b4ffba32fef584` 下真实前瞻评估 `scored_n=158`、`pending_n=180`、`result_conflicts=0`；three-way/total-goals/scoreline/half-full 均有有效指标。
- 评分审计保留 freeze records、fixture identity、cutoff、observed_at、model lock 和 raw provenance；结果选择标志 `results_not_used_for_selection=true`。
- 代表性 fixture：`openfootball:bundesliga:72a51641d41799204d13603c`，开赛 `2026-09-04T18:30:00Z`，赛果 `4-1`，OpenFootball raw SHA 在运行快照中保留。

### 5. 正式生产门槛 — **Unmet（正确关闭）**

- 当前明确为 `production_allowed=False` / `promotion_eligible=False`；没有降低门槛或用历史回测替代前瞻样本。
- 样本短缺：three-way `158/1000`，total goals `158/1000`，half-full `144/1000`，scoreline `158/5000`。
- 其他阻断：`prediction_freezes_verified=False`、`market_baseline_status=unavailable_no_independent_market_baseline`、`result_admission_clean=False`。线上 facts 与 research API 的 formal probabilities 均为 false。

### 6. 运行可靠性 — **Met（针对本轮隔离 facts writer；完整研究 runtime 仍受生产门槛限制）**

- VPS 最终状态只启用 `matchline-research-facts-cycle-r9.timer`；r7/r8、system-level legacy 和 user-level legacy facts timers 均 disabled/inactive。
- r9 service 最近成功退出 `0`；facts-only collector → publisher 链路发布 HTTP `201`，输入 hash 与 current hash 一致。
- r9 隔离演练归档 SHA `9609f4f59bc8820382d09e351c48a677cb6b5c11b1273be0ef45eb67d0d5802e`，current 与 raw manifest 恢复后哈希均一致；实际执行了 r8↔r9 timer rollback switch 后恢复 r9。
- 既有 Python runtime archive/checkpoint/restore 幂等测试均通过；旧 `/home/ubuntu/matchline-facts` 未由新 r9 writer 写入。

### 7. 验证与发布 — **Unmet（验证通过，Sites/GitHub 最终发布仍受认证阻断）**

- Python 全套：`1516 passed, 72 skipped, 0 failed`；新增 collector/publisher 回归 `22 passed`；Sites `npm run typecheck` 通过，`npm test` `808 passed, 0 failed`；父/Sites `git diff --check` 通过；r9 systemd units `systemd-analyze verify` 通过.
- VPS 已有真实 facts publish/readback：最新 facts readback HTTP 200，包含 finished OpenLigaDB 案例与 blocked source catalog 案例；formal probabilities false。
- 父 GitHub 非 main 分支已确认 `18c1bfa`；Sites `9d424d0` 未推送（远端缺 Git 认证），Cloudflare deploy 未执行（token expired/missing）。

### 8. 交接资料 — **Met**

- 本报告、r2 逐源 JSON/Markdown、r2 probe、facts refresh projection/evidence、prospective evaluation/audit、VPS r9 release/readback、raw archive/restore/rollback 和机器验证摘要均已写入 `docs/` / `docs/evidence/`。
- 可复现命令：
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m league_platform.source_research --snapshot docs/evidence/source-research-input-2026-09-16-r2.json --probe-evidence docs/evidence/source-probe-evidence-2026-09-16-r2.json --observed-at <probe-response-time> --output-json docs/evidence/source-research-2026-09-16-r2.json --output-markdown docs/source-research-2026-09-16-r2.md`
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`
  - `cd matchline_sites && npm run typecheck && npm test`
  - `git diff --check`；`cd matchline_sites && git diff --check`
- VPS rollback：`sudo systemctl disable --now matchline-research-facts-cycle-r9.timer; sudo systemctl enable --now matchline-research-facts-cycle-r8.timer`。

## 仍未解决问题

1. Sites 子仓库远端认证缺失，无法推送 `9d424d0`；Cloudflare API token 缺失，无法部署该 Sites 修复。
2. Sites 子仓库 `9d424d0` 仍未推送；父仓库 `c9d43bf` 已推送并可审阅。
3. 正式生产门槛仍缺足够 untouched prospective samples、lineup confirmation replay 和独立 market baseline；正式概率必须继续关闭。
4. r9 已完成允许来源逐响应 raw archive；后续新增来源必须先通过同等 rights/host/raw archive gate。
5. r9 release 已激活 facts-only writer；prospective audit 仍独立于 facts publisher，正式预测未发布，线上 prospective status 继续保持关闭，符合不伪造原则。
