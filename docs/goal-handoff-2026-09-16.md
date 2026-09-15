# Matchline Goal 交接报告（2026-09-16）

本报告对应 `.pi/goal/完成-matchline-研究闭环并建立合规数据源抓取能力-20260916-0219.md`。本轮未删除、reset 或清理原有工作区改动，也没有覆盖 VPS 既有 facts 环境。

## 结论摘要

- **技术研究闭环：已成立。** 本地 runtime-only 周期在现有候选锁下产生了真实完赛评分：`scored_n=158`，`pending_n=180`，无结果冲突；OpenFootball fixture identity、cutoff、结果观察和 SHA provenance 均保留。
- **逐源审计：已落地。** 新报告覆盖 registry 与 `SourceId` 并集共 47 条来源/执行层记录；每条均有权利结论、条款/许可证引用、robots/access、allowlist/redirect/size/timeout、限速、解析、字段 declared/observed/missing、实体联结、双时钟、SHA 策略、实际状态、替代方案和 eligibility。
- **正式生产门槛：未通过且继续关闭。** 当前评估明确为 `production_allowed=false` / `promotion_eligible=false`，没有以改门槛、补零或历史回放伪造通过。
- **VPS：已完成隔离 staging 与演练，未激活。** 新 release 已上传并通过 SHA、私有 venv、report CLI、runtime-only、archive、verify、restore；旧 `/home/ubuntu/matchline-facts` 未覆盖。VPS 没有当前研究 service 的 active writer，因此尚未宣称服务器自主生产运行。

## 证据索引

| 证据 | 位置/摘要 |
|---|---|
| 逐源机器报告 | `docs/evidence/source-research-2026-09-16.json`，schema `matchline.source_research.v1`，47 条；JSON SHA `3d07b47e143c67c06e3e0d97948d4b545add9095cc46ea1517ffe6d9934bbc27`；快照 SHA `b064d56c32cd588ccf134bb155876b418d52bb6407d0502250ff5072defacf89` |
| 逐源可读报告 | `docs/source-research-2026-09-16.md`；Markdown SHA `9f51461e257928c76c20d287a4f85b315f91b8bf3caf2bb7751300215cf5b510` |
| 固定端点 probe | `docs/evidence/source-probe-evidence-2026-09-16.json`，4 条 probe；SHA `ca5d608b04dc37549598c3bf1345278f01e0c6977c26cc3e9b6a60cec6ac0c35` |
| 可复核 snapshot 副本 | `docs/evidence/source-research-snapshot-2026-09-16.json`，SHA `b064d56c32cd588ccf134bb155876b418d52bb6407d0502250ff5072defacf89` |
| 当前研究评估 | `/dev/shm/matchline-live-runtime/runtime-only-evaluation-current.json`；外部最新 checkpoint `checkpoint-a6513c94d839ebde7785` 同步保存关键 ledger |
| 最新本地 D1 审计上传 | `auditId=335`、`currentFreezeCount=338`、`scoredN=158`、`pendingN=180`、`requests=1`、`productionAllowed=false`、ACK status `ok` |
| 当前模型锁 | `/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/candidate-locks/prospective-model-lock-v260-20260904T-luna-max-current-v2.json`，SHA `8578af943bf842f2e084bbfebfaac6cb5bd86023efdbceb06f6ee2f956b37fc6` |
| VPS release/演练回执 | `docs/evidence/vps-release-drill-2026-09-16.json`；远端 final release `/home/ubuntu/matchline-releases/matchline-research-20260915T2120Z-faa8ff8-r4`；archive SHA `8e06d992…`，restore `restored`，release `active=false` |
| 当前模型版本 | SHA `357a2d4c1082552ef8515e4fee327e75243f5dca4aa69d0047b4ffba32fef584` |

## Acceptance criteria 逐条验收

### 1. 逐源深度研究 — **Met**

- 报告以 `registry_union_SourceId` 枚举 47 条，inventory digest 为 `ed3d9fce05ed159c99b7657d61d9a7c4595b4618510678f9f129bc2d45aa7fc1`。
- 每条记录具备完整固定字段；校验器拒绝重复 ID、未登记 probe、根级/行级 `unknown`、坏 SHA、时间逆序和受限来源网络成功声明。
- 已核对的官方资料包括：OpenFootball CC0、OpenLigaDB API/ODbL、Wikidata CC0/access etiquette、MET Norway CC-BY/UA/traffic、Open-Meteo free-tier commercial boundary、Disney/ESPN automated extraction restrictions、Sports-Reference data-use restrictions、WhoScored license requirement、Premier League/LaLiga/Serie A/Ligue 1 terms、Bundesliga robots/legal notice。
- 本轮真实 HTTP probe：OpenFootball `200 text/plain`；OpenLigaDB `200 application/json`；Wikidata `200 application/json`（gzip wire/content SHA）；MET response reached JSON parser but exact forecast-hour contract failed，未产生天气记录。

### 2. 权利明确来源的端到端接入 — **Unmet（已部分完成）**

- 已有真实端到端证据：OpenFootball `1257` 当前 rows、OpenLigaDB `306` display/post-match rows；对应原始 hash、时间、解析/联结测试和 runtime projection 已通过。
- Wikidata 取得过真实 2 场/6 请求的实体→场馆结果，且有 raw SHA；但当前生产 snapshot 仍为 `not_configured`，该 probe 没有进入 durable current read model。
- MET 真实响应因没有精确目标小时被结构化降级；没有伪造天气行。
- 因此不能宣称“每个权利明确且成功来源都已发布到当前读模型”。

### 3. 受限来源处理 — **Met**

- ESPN、SofaScore、官方首发、体彩、Understat、OddStorm、Open-Meteo commercial free tier、Crawl4AI 未证权利页、FBref/WhoScored/ClubElo/SoFIFA/FotMob 等在报告中保持 `rights_blocked`、`not_attempted`、`quarantined`、`blocked_by_robots` 或 `not_configured`。
- 报告显示网络实际打开的来源只有 `openfootball_current`、`openligadb_secondary_results`、`wikidata_entities`、`met_norway_weather`；受限来源没有被 probe 覆盖为成功。
- OddStorm 的 cycle 回执明确 `network_opened=false`、`rights_status=blocked_pending_express_written_permission`；旧反 WAF relay 仅保留负面审计记录。

### 4. 真实前瞻闭环 — **Met**

- 最新本地 runtime-only 周期结束于 `2026-09-15T21:02:53.669599+00:00`，评估生成于 `2026-09-15T21:02:14.398355+00:00`。
- 当前锁下 `scored_n=158`，`pending_n=180`，`archive_conflicts=0`，`result_conflicts=0`。
- 158 条可评分行与 OpenFootball 完赛结果精确联结；示例：`openfootball:bundesliga:72a51641d41799204d13603c`，开球 `2026-09-04T18:30:00Z`，冻结 cutoff `2026-09-04T12:30:00Z`，fixture 首次观察 `2026-09-04T12:10:10Z`，赛果 `4-1`，原始源 SHA 保留。
- 评估输出有效 three-way/total-goals/scoreline/half-full metrics；没有使用赛果做模型选择。
- 该技术闭环仍是 research-only，不代表生产门槛通过。

### 5. 正式生产门槛 — **Unmet（按合同继续关闭）**

真实评估文件明确：

- `production_allowed=false`、`promotion_eligible=false`、`sample_requirements_met=false`；
- three-way `158/1000`，shortfall `842`；
- total-goals `158/1000`，shortfall `842`；
- half-full `144/1000`，shortfall `856`；
- scoreline `158/5000`，shortfall `4842`；
- `prediction_freezes_verified=false`：当前锁有 T−24/T−6/T−90，但没有可验证的完整 `lineup_confirmation`；
- `market_baseline_status=unavailable_no_independent_market_baseline`，market sample `0`；
- `result_admission_clean=false`：结果候选中仍有被明确隔离的非 OpenFootball/未验证 rows；它们没有进入 158 条已评分主样本。

严格历史报告虽有 `combined.scoreline_sample_n=21098` 且模型优于 frequency baseline，但它是反复检查过的 development backtest，不是锁定后的 untouched prospective test，不能代替以上门槛。

### 6. 运行可靠性、恢复与单写入者 — **Unmet（演练已完成，生产迁移未完成）**

已完成：

- 本地周期成功完成 checkpoint；最新关键 ledger 在外部 `checkpoint-a6513c94d839ebde7785` 保存。
- VPS final release `drill` 完成真实 runtime-only 周期（OpenFootball `1257` rows、OpenLigaDB `306` rows、`result_admission.quarantined_rows=0`），并完成 `runtime_archive`→SHA verify→`runtime_restore`：`120` files、`31,675,120` bytes、archive SHA `8e06d9928fa456d57169540fd5c6a69064b5285c93a2e24af63808c5143274e5`，restore status `restored`。
- 不完整 archive drill 被 restore 正确拒绝（缺 `current.json`），失败回执保留。
- VPS final release 无服务激活，旧 facts 目录仍存在；因此“VPS 上唯一明确的当前研究写入者”尚未成立，不能宣称跨重启生产运行。

### 7. 验证与发布 — **Unmet（验证通过，正式发布/推送未完成）**

已通过：

- Python：最终全套 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q`：`1513 passed, 72 skipped, 0 failed`，耗时约 09:40；4 个既有 multiprocessing warning。
- Sites：`npm run typecheck`、`npm test`：`807/807 passed`；build 和 32 项 build-asset verification 通过。
- `systemd-analyze verify` 目标 history service/timer 返回 0；仅有系统内其他 unit 的既有 warning。
- VPS final release：source tar SHA `b8a8bab2707da86d389f4e20c5e3ee339ab3372c3a1509f4f4bae71b0f230bab`、model lock SHA `8578af...`、raw bundle SHA `4f7f20d414f760b95fab3dd0ddfdff63512f22e05d7f45e6dd8bc8d72027880c`，final source report 47 条校验通过。

未完成：

- final VPS release 保持 `active=false`，没有安装/启用当前研究 timer；VPS 只有旧 facts timer，不能提供“发布后的 current research service state”。
- 本轮未用 final VPS release 对真实 D1 endpoint 执行上传；本地现有 runtime 的真实上传 `auditId=335` 已成功，但它不是 final release 的远端 active service ACK。
- GitHub 父仓库非 `main` 分支已提交并推送：`codex/crawl4ai-source-adapters` → `27d0b1c1446c5351ccc5656bf349a70444820e55`；Sites 子仓库本地 commit `dd8e80d` 已创建，但 `sites` 远程因缺少认证未推送。

### 8. 交接资料 — **Met**

本报告、逐源 JSON/Markdown、probe evidence、可复核 snapshot、VPS release manifest/演练路径和命令均已整理。仍未解决项与后续动作如下：

1. 获得书面授权或采用明确商业许可的首发/市场源；在此之前不打开受限 source。
2. 继续当前锁的真实前瞻窗口，直到四类目标及各联赛样本达到固定门槛；不能用历史回测替代。
3. 将 VPS release 放入持久 ext4/XFS runtime root，安装正确的 systemd environment，完成 restore→runtime-only→archive→upload→rollback 后再停本地 writer。
4. 为最终 release 配置真实、受保护的 D1 audit endpoint/token，执行一次真实 ACK，并确认线上源码 SHA 与 final source tar 一致。
5. 如需同步 Sites 子仓库的两项相对日期测试修复，先配置 `sites` 远程认证，再推送其 `codex/goal-research-loop-20260916` 分支；父仓库其余未提交改动仍不得全量 add。

## 可复现命令

```bash
# 来源审计（显式输入时间，报告生成器不联网）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m league_platform.source_research \
  --snapshot docs/evidence/source-research-snapshot-2026-09-16.json \
  --probe-evidence docs/evidence/source-probe-evidence-2026-09-16.json \
  --observed-at 2026-09-16T05:08:43+08:00 \
  --output-json docs/evidence/source-research-2026-09-16.json \
  --output-markdown docs/source-research-2026-09-16.md

# Python 全套
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q

# Sites
cd matchline_sites
npm run typecheck
npm test

# systemd 模板
systemd-analyze verify \
  deploy/systemd/matchline-openfootball-history-refresh.service \
  deploy/systemd/matchline-openfootball-history-refresh.timer
```
