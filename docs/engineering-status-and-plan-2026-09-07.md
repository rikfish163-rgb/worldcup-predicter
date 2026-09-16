# Matchline 工程现状、差距与后续执行计划

核查时间：2026-09-07 12:13–14:55，Asia/Shanghai。本文是一次工程交接快照，后续数据数量会变化。

本文前半部分保留了 12:13 的问题快照；若与“最新收束复核”冲突，以最新收束复核为准。14:37–14:55 已完成小范围修复、验证、审计上传和网站 v200 发布。后续接手先读最新收束复核，再检查任务涉及的少量实时证据，不重做全项目探索。

## 最新收束复核（2026-09-07 14:55）

### 已落地并验证

- Sites 子仓库已提交并推送 `26436cd95283236f4601e6ccee16719d7b0cd7d8`，部署版本为 v201，公网部署成功：`https://matchline-intelligence.willif57kbkd.chatgpt.site`。上一版 v200 的 `84aa5c04f57a7630dcb5782bdfe536cc0dadf7cd` 仍可作为回滚点。
- 研究预测接口已统一保留 `remote_facts` 模式；公网抽样返回 `x-matchline-data-mode: remote_facts`、`formalProbabilities: false`、`productionReady: false`，不再把事实桥误标成 `research_snapshot` 或伪造正式概率。
- `/api/matches` 仍按公开边界剥离原始 `researchPrediction` 对象，只保留状态与观察时间；前端据此生成同一份非数值审计卡。`/api/v1/research-predictions` 则返回受 allowlist 约束的 `research_draft` 审计信封。两者都不得包含正式概率；不要再次把“剥离原文”误判成数据读取失败。
- `/api/prospective-status` 已把最近审计的 `freezeRecords` 传入事实投影，列表/详情可表达逐场研究冻结证据。公网审计上传已成功确认：`auditId=167`、`created=1`（首次发布），`currentFreezeCount=150`、`pendingN=150`、`scoredN=0`、`productionAllowed=false`；重复执行返回 `created=0`，说明幂等生效。
- 本地 systemd 上传单元的 User-Agent 引号已修正，并通过 `systemctl --user show` 和实际执行日志验证；最近执行 `status=0/SUCCESS`，单次 `requests=1`。
- UI 只做了收束性修复：事实行保留可访问状态标记但隐藏重复视觉圆点；移动端 facts-only 比赛行压缩为三轨；meta/technical/provider 文本最低提升至 11px/10px，减少 0/重复状态造成的噪音。
- Sites 验证：`npm run typecheck` 通过；全套 `npm test -- --runInBand` 通过（807 tests）；新增冻结审计回归后定向 23/23 通过，`git diff --check` 通过。父仓库发布/上传相关回归 16 项通过。

### 当前真实边界

- 当前仍是事实/研究审计产品，不是正式预测产品：`scoredN=0`、`productionAllowed=false`，所有概率和市场字段继续关闭；`researchPrediction` 里的对象是审计元数据，不是数值预测。
- 当前主要事实链不是只有 OpenFootball：公网快照同时可见 OpenLigaDB；OpenLigaDB 的 2,112 场/885 条进球事件是事实显示用途。MET Norway 当前只有城市级观测，尚未完成逐场场馆关联。
- Football-Data.co.uk 的 8 个固定入口当前均为 HTTP 失败；ESPN/SofaScore/Understat 等受访问策略或授权边界限制。失败和未授权必须继续显示为明确不可用，不能填 0。
- VPS `170.106.198.250` 目前仍是旧的 `wc_analysis` 运行环境，尚无可直接启动当前研究周期的完整 release bundle、归档和浏览器运行时。本轮没有在 VPS 上覆盖或改写任何文件。
- 本轮无法用 Playwright 对公网页面取得可信截图：边缘返回 Cloudflare 403。因此不把历史 iteration84 的 95 分当作当前视觉分数；当前只认定功能/API/构建回归通过，视觉 90 分仍待同一数据和视口的可复现截图验收。

### 接下来只保留三条主线

1. **研究闭环（唯一 P0）**：先保持现有本地周期单写入者，等本轮周期明确结束后核对已完赛冻结记录为何仍为 `scoredN=0`；补齐结果身份、观察时间和评估命中证据。只有 `scoredN>0` 且门槛通过，才开放正式概率。
2. **服务器迁移（P1）**：为 `170.106.198.250` 打包当前代码、模型锁、OpenFootball 原始归档和 ext4 运行目录，先做 restore→runtime-only→archive→upload 的单周期演练；验收跨重启恢复后再停本地写入者。禁止直接把本地 tmpfs 或整份缓存复制过去。
3. **产品收敛（P1）**：冻结一个主流体育平台作为结构基准；只围绕导航、筛选、比赛行、状态标签、空状态、详情和移动端做一轮截图验收。数据源按字段补：先 OpenLigaDB 事实/赛果，再做场馆身份→MET 关联；被 403/503/授权阻断的源只保留诊断，不重复刷请求。

以下工作明确不再重复：重新寻找参考站点、继续追加全局 CSS 覆盖、把 `remote_facts` 改名为预测、用历史赔率填当前赔率、为了消除红色而降低正式预测门槛、或在 VPS 上直接覆盖旧 `wc_analysis`。

## 1. 当前结论

工程已具备可公开访问的足球事实数据台，以及本地运行的研究与审计模块。距离用户要求的服务器自主运行、信息充足、视觉统一的足球预测产品仍有明显差距。

主要断点是：事实采集与研究运行分处 VPS 和本地；研究发布与比赛读模型未闭环；部分数据源只有适配器或失败诊断；UI 长期追加补丁；旧状态文档与现场不一致。不能用接口可达、已有代码、候选数量或历史视觉分数证明产品完成。

## 2. 已验证的成果

| 能力 | 本轮权威证据 | 当前边界 |
| --- | --- | --- |
| 公网访问 | `https://matchline-intelligence.willif57kbkd.chatgpt.site` 的事实、比赛、审计和服务状态接口均可读取 | 页面交互、移动端和自定义域名本轮未重新验收 |
| VPS 自主采集与发布 | `ubuntu@170.106.198.250` 两个 facts 服务最近执行成功，两个 timer 有下一次运行时间 | 仅证实事实链已迁移；研究计算仍在本地 |
| 当前赛程 | 公网汇总 OpenFootball 2,916 行，OpenLigaDB 2,112 行 | 不是去重后的比赛总数，两个来源可能覆盖同一比赛 |
| 历史数据 | OpenFootball 8,763 行，涵盖 2023-24、2024-25、2025-26 | 是来源记录数，不等同模型已使用这些记录或完成评估 |
| 赛后事件与补充信息 | OpenLigaDB 885 条进球事件、天气 12 条、Wikidata 4 个实体 | 事件不等同全站实时覆盖；天气不等同每场已关联 |
| 来源观测 | 54 个配置条目：44 fresh、8 failed、2 blocked | 是联赛/赛季/接口条目，不能称为 54 个独立供应商 |
| 积分榜与球队接口 | `/api/standings` 返回 `ok/openligadb_current`；`/api/teams?team=Arsenal` 返回 `ok/remote_facts` | 已确认接口路径有效，完整内容和交互仍需逐项验收 |
| 研究运行 | 最近完整本地评估：pending_n=146、scored_n=0、production_allowed=false | 146 是冻结记录数，不是 146 场比赛或 146 个有效预测；本轮有新周期正在运行 |
| 稳定比赛身份 | 本地 collector 与 VPS collector SHA-256 相同：`329f2e2767827491a04d0f3676346e0dfcea3bcc80f65394716d435163a00740` | 上一轮已改 canonical ID；关联命中率仍需端到端验证 |

事实接口采样 `asOf=2026-09-07T04:03:43.000Z`。核心来源信息来自 `/api/v1/remote-facts?source=all&scope=all&limit=1` 的 `sourceSummary`。

## 3. 现在的问题和根因

### P0：研究状态仍未形成可用闭环

- `/api/matches?scope=upcoming&horizon=7d&limit=200` 返回 137 行，全部 `researchPredictionState=unavailable`，原因均为 `model_release_not_published`。这直接解释了用户看到的大量重复不可用状态。
- `/api/prospective-audit` 仍返回旧签名候选审计：144 条 pending，0 条 scored；`asOf=2026-09-06T14:43:10.381Z`，没有 `freezeRecords`。
- 本地最近完整评估为 146 条 pending，生成于 `2026-09-06T15:48:06.764136+00:00`；完整周期结束于 `15:49:21Z`。公网审计没有追上本地结果。
- 上一轮上传收到 HTTP 422。代码证据支持新增 `freezeRecords` 与公网旧契约不兼容的解释，但尚未读取并确认该次 422 的响应错误码；不能把推断写成已完成根因验证。
- `/api/prospective-status` 当前返回 `remote_facts`，pending/scored 为 null；其实现会在远端事实比候选审计新至少 6 小时时回退。与审计接口的区别有代码依据，但用户看不出“事实新鲜、研究陈旧”两个状态。
- 新的读取 helper 吞掉数据库/校验异常并返回 null，后续需保留内部可诊断原因，避免再次把读取失败与未发布混为一谈。

### P0：未上线补丁本身还需复核

Sites 子仓库 HEAD 为 `d56c6b1`，有 11 个已跟踪文件的未提交改动，179 行新增、38 行删除：

- `app/api/prospective-audit/{contract,route}.ts`：逐场冻结元数据契约及读取。
- `app/remote-facts-match-utils.ts`、`app/api/v1/remote-facts/{projection,route}.ts`：事实与冻结记录关联。
- `app/api/matches/route.ts`、`app/api/matches/[fixtureId]/route.ts`、`app/api/matches/remote-facts-detail.ts`、`app/page.tsx`：列表、详情和首屏读取联动。
- `app/MatchDetailView.tsx`、`app/ProspectiveStatusPanel.tsx`：冻结状态和时间文案。

Python 发布端 `league_platform/publish_prospective_audit.py` 已添加逐场元数据；文件在父仓库仍是 untracked，不能因普通 `git diff` 不显示就忽略。

发布前必须核对以下具体风险：

1. 当前 `Boolean(freezeRecord)` 就把 `researchPredictionState` 标成 available，但 `researchPrediction` 仍为 null。应明确表达“冻结证据存在”，不能让用户理解为预测结果可用。
2. 按 fixtureId 构建 Map 会让同场多阶段记录覆盖，必须规定选取规则，并核对开球时间调整、联赛、模型版本和陈旧记录，避免串联旧冻结。
3. 新 TS 元数据校验检查了 cutoff 早于 kickoff，但仍需核对观察时间、记录唯一性、汇总计数一致性和完整时间合同。
4. Python 元数据上限为 1,024，超限会拒绝整次发布。必须明确窗口、汇总和逐场分页/截断设计，防止持续运行后再次整体不可用。

### P0：发布控制面待恢复

本轮对 manifest 中既有 project_id 调用 Sites get_site 和 list_site_versions，均返回 `project_not_found`。公网仍正常，不能据此判断站点被删除。应检查当前账号、工作区和项目绑定；不要创建替代站点或覆盖 `.openai/hosting.json`。

历史交接记录称版本 198 已部署，但本轮无法通过控制面复核当前生产 commit/version。目前只能确认公网行为与本地未提交新契约不一致。

### P1：研究运行仍依赖本地机器

- 本地 `matchline-prospective-cycle-runtime-only.service` 在 12:15 核查时仍为 activating/start，MainPID=21235；这是正在运行的周期，本轮没有中断或重启。
- 本地 `/dev/shm/matchline-live-runtime` 占用约 7.4 GiB。它是本机 tmpfs，占内存/可能涉及交换空间，并非服务器持久存储。
- VPS `/home/ubuntu/matchline-facts/current.json` 约 11 MiB，facts 目录约 11 MiB。事实已迁移不代表研究档案、冻结和评估都已迁移。
- 审计上传服务的 drop-in 存在未正确引用的带空格 `Environment=` 值。systemd 日志明确报告忽略多个 UA 片段；需修正并实际验证加载值。这不是已证明的 422 根因。
- 重启后 oneshot 显示 inactive/success 不能证明最近上传成功；验收应读取执行时间、ACK 和公网快照。

### P1：数据缺口要按字段定位

| 缺口 | 已确认现状 | 下一步证据 |
| --- | --- | --- |
| Football-Data.co.uk | 8 个联赛配置均 `http_error`，recordCount=null | 每个固定 URL 的状态码、响应类型、赛季路径、解析行数；先复用现有适配器 |
| ESPN / SofaScore | VPS 诊断为 `provider_policy_or_access_block` | 分清访问限制、实现策略和授权要求；不要只重复同一失败请求 |
| 首发/伤停 | 公网抽样比赛无首发适配器，计数为 null；本地上次周期 lineup_observed=0 | 按场测试来源发现、实体匹配、解析和开赛前观察链 |
| xG/球队深度统计 | 已有研究模块与文档，不代表当前公网有数据 | 核查选定球队的实际返回、时间和来源，然后决定补适配或补读取 |
| 当前赔率与预测 | 研究预测接口 `mode=unavailable`，正式概率保持关闭 | 先确认可用来源与模型产物，再验证发布；不能拿历史赔率充当当前赔率 |
| 比分与事件 | OpenLigaDB 已有部分真实事件 | 在已有返回上完成列表/详情一致显示，明确具体联赛覆盖 |

建议每个源只维护一条诊断记录：来源/字段/联赛/URL、运行位置、最后尝试时间、HTTP/解析/关联结果、发布结果、下一动作。用户给的 UI 复刻仓库是设计工作流参考，不能据此推断获得更多足球数据权限或接口。

### P1：视觉与产品结构欠收敛

- `app/globals.css` 9,554 行，`LiveBoard.tsx` 2,345 行；字体变量在多个后段用 `!important` 覆盖。
- 仍有 8–9px 的诊断文字；这不符合用户要求的清晰、舒适字体。单独再追加字体规则很可能继续互相覆盖。
- 本地视觉产物约 89 MiB。现存 verdict 文件自报 95 分并引用旧版本；本轮未重拍页面，因此不认定最新公网达到 90 分，也不认定已复刻完用户要求的桌面和移动端。
- 现有状态和技术诊断大量进入主界面，用户的核心任务“找比赛—看事实—看分析结果”被稀释。
- 设计方向：固定一个主流体育平台作结构基准，使用用户提供的字体截图作为字体风格约束；明确导航、筛选、比赛行、详情、空状态和移动端六类模板。事实信息与分析信息有清楚入口，技术诊断放入二级展开区域。

### P2：文档和版本管理造成重复工作

- `docs/commercial-readiness.md` 原正文停留在 2026-08-26，许多数量与当前不符；`docs/current-data-and-prediction-contract.md` 同时包含架构合同与过去运行结论。
- 父仓库当前有 110 条已跟踪改动/删除状态，还有大量未跟踪文件；Sites 是独立子仓库。不能全量 add、reset、清理或把父仓库提交视为网站已发布。
- 普通 `python` 指向 Conda 且本轮前序导入失败缺 pandas；systemd 用 `/usr/bin/python3`。复现优先使用真实服务解释器，避免误判工程依赖坏了。

## 4. 后续执行顺序和验收标准

| 顺序 | 工作包 | 完成证据 | 不应重复的工作 |
| --- | --- | --- | --- |
| 1 | 恢复发布项目可见性，审查并完成既有冻结关联补丁 | 能读取既有项目；新旧审计契约兼容；错误可诊断；验证后部署准确源码 | 不新建站点、不重写已有 11 文件补丁 |
| 2 | 修复上传配置，完成一场比赛的端到端闭环，再扩大覆盖 | 发布 ACK 成功；公网审计追上已结束周期；选定同场列表/详情/状态接口时间和阶段一致；无预测时含义明确 | 不靠批量改文案或修改门槛消除红色 |
| 3 | 将研究计算与归档迁至 VPS | VPS 独立完成采集→冻结→评估→上传；断开本地后跨周期正常；持久档案与恢复可验证 | 不复制整份本地运行缓存；不同时留两个写入者 |
| 4 | 补字段覆盖，先赛事/赛果/球队状态，再首发伤停/xG/市场 | 每类至少一场真实证据走通，然后报告支持联赛、已覆盖场次/应覆盖场次、失败原因 | 不把新增源文件数或 HTTP 200 数量当产品成果 |
| 5 | 固定设计基准，收敛样式并验收核心交互 | 同一数据与视口的前后截图；导航、筛选、比赛行、标签、空状态、详情、移动端逐项检查；可追溯 visual-verdict≥90 | 不继续换参考、不复用旧分数、不追加另一套全局覆盖 |
| 6 | 上线验收 | 已发布版本、数据新鲜度、跨周期运行、核心流程、失败降级和回滚均有证据 | 不以单次测试通过宣称完整预测产品成熟 |

第 4、5 项可在第 2 项闭环后分范围推进，但同一组件/数据契约保持一个实施负责人。研究预测的真实结果和验证必须继续做，不能把“事实数据台已可用”替代整个产品目标。

正式预测质量验收单列：当前 scored_n=0、prediction_freezes_verified=false。必须查清已冻结记录为何没有有效评分，核对已完赛样本、赛果身份、观察时间、准入和锁一致性；若确实未到窗口，记录具体日期和样本，而不是泛称等待。不得为了发布直接降低样本门槛。

## 5. 接手清单与验证口径

1. 先检查当前运行周期是否结束；检查其明确 PID/服务状态，不因等待超时重复启动。
2. 对照本文的 11 文件未提交补丁，保留原改动，先审查关联语义和契约，再写必要回归测试。
3. 只在新修改完成后运行相关测试与构建。历史上下文曾报告 collector 13 项及 Sites 805 项通过，但并非全部覆盖最后一次改动，本轮不重新认定这些结果。
4. 本轮 `git -C matchline_sites diff --check` 无输出通过，仅说明补丁没有该检查识别的空白错误，不代表功能正确。
5. 发布后记录生产源码、接口采样时间、一场可用及一场不可用案例、桌面/手机截图、仍未解决的问题。更新本交接入口，避免新增大量互相矛盾的“最终报告”。
6. 本轮已做的只读接口包括：`/api/prospective-audit`、`/api/prospective-status`、`/api/matches`、`/api/v1/remote-facts`、`/api/v1/service-status`、`/api/standings`、`/api/teams?team=Arsenal`、`/api/alerts`、`/api/v1/research-predictions`。返回路径与模式已验证，未逐一断言所有业务字段完整。

本轮未删除、迁移或清理任何历史证据，也未更新个人记忆。保留所有未解决问题，完整产品目标仍未完成。

## 6. 文档生成后的运行复核

13:32（Asia/Shanghai）再次读取服务状态时，`matchline-prospective-cycle-runtime-only.service` 仍处于 `activating/start`，MainPID=98367；周期文件更新时间为 13:32:11，评估文件更新时间为 13:31:44，说明本机又开始了一轮计算。`matchline-prospective-audit-upload.service` 此时为 `failed/exit-code=1`；因此不能把本次候选周期说成已发布到公网。下一次接手应等待该 PID 进入明确终态后，先取失败日志和上传响应，再决定是否重试，不能连续启动多个周期。
