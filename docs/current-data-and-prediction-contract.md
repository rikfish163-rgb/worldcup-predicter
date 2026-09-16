# 当前数据与未来预测契约

> v260 权利清理主线 / v277 前瞻锁（2026-08-26）：正式当前赛程、赛果和历史训练只从
> OpenFootball CC0 的固定 allowlist 与 durable raw archive 进入。中足联、ESPN、官方首发、赔率、
> xG、新闻和其他公开页面即使可访问，也必须先通过独立来源权利与 provenance 准入；当前未通过的记录
> 只保留为阻断/隔离诊断，不进入 canonical fixture、模型输入或公开再分发。只有精确开球行进入
> canonical fixture，只有日期的行继续归档但隔离。每条 OpenFootball row 保留 provider fixture/team ID、
> 双时钟、原始哈希和确定性身份；Sites/离线包不把其他来源重标成 OpenFootball。
> 任何官方赛程、场馆或首发 overlay 都必须同时满足来源权利、严格赛事/主客队联结、字段级 provenance
> 和 cutoff；未满足时保持缺失，不以主队、主场惯例或字符串相似度猜测。
> OpenLigaDB 仅作为德甲独立 ODbL display/post-match 交叉源，不是 canonical authority、训练源或再分发源。
> 到达开球窗口或 provider 元数据更新时间不能单独证明比赛正在进行；没有实际事件时必须保持
> `in_progress_window`，Sites `live=false`。
> ESPN 当前适配器、历史 ESPN serving、OddStorm 市场页以及权利未知的 Crawl4AI 页面在网络访问或展示前
> fail closed。OddStorm 官方条款明确限制自动抓取、镜像、再分发和转售；没有可核验的书面授权引用时，
> 当前页面与 history endpoint 均不得发起网络或文件轮换。OpenFootball 只对 TLS/连接/超时等瞬时传输失败
> 做至多一次同源重试，不重试 HTTP 拒绝、重定向、超限响应或解析错误。
> 研究窗口固定为未来 7 天；正式冻结还必须同时满足比赛在活动评估窗口内、fixture/特征在 cutoff 前已
> 观察、且 `freeze_cutoff_at >= evaluation_window_started_at`。模型版本在冻结点之后才锁定时禁止回填，
> 即使比赛尚未开赛也只能显示研究草稿或结构化阻断。

严格历史报告的命令行默认也不加载任何可选开球时间增强。自动任务把当前报告原子写到运行目录，
并以内容 SHA-256 追加保存不同版本；不会覆盖仓库里的 dated evidence，也不会直接改 Sites `public/`
或触发发布。旧 ESPN 时间增强产物只保留为 legacy audit，除非未来存在明确书面授权并由操作者显式传入，
否则不能再次进入严格报告。历史形式化报告只作为外置归档中的审计材料；实际定时任务把同一协议的报告写到 runtime
`strict-backtest-current.json`。报告、前瞻窗口和
publication audit 仍为 `production_allowed=false`，不能仅凭报告状态发布为商品预测。

## 不可混淆的两层

1. `data/MatchHistory` 中的 football-data.co.uk 与 OpenFootball 数据只用于训练、校准和回测。
2. runtime `current.json` 只保存带 `as_of/retrieved_at` 和内容哈希的当前来源快照，用于确认未来
   fixture 和构造预测时点特征。生产 unit 使用 `/dev/shm/matchline-live-runtime`；仓库下的
   `data/live` 仅保留指向外置归档的兼容 symlink，不是当前生产指针。每个 HTTP 响应在实际收到后
   记录 `retrieved_at`，最终 `as_of` 在全部来源完成后生成；因此所有被采用的观察都满足
   `retrieved_at <= as_of`。

历史比赛不会因为仍在本地而被标成“当前”。服务会在每次 snapshot/health/predictions 请求时重新
读取并校验当前快照；超过 6 小时后预测 API 自动变为 unavailable，不依赖进程重启。核心赛程仍新鲜但某个
可选或次级源失败时，快照为 `degraded`：预测 API 可以返回研究型概率，但必须携带当前状态、缺失字段、来源错误
和覆盖等级；不会把降级状态标成生产可用。只有 `stale`/`unavailable` 才全局阻断未来预测。

## 多源职责

> 说明：下表保留已实现的适配器和研究观察合同，便于审计与后续重新申请权利；“存在适配器”不等于
> “本轮已联网或可公开发布”。当前 v260 策略只有 OpenFootball 事实链进入正式 current/model/publication，
> OpenLigaDB 只允许隔离的 display/post-match lane；其他来源若未通过独立 rights/provenance gate，均为
> `rights_blocked`、`unavailable` 或 quarantine。source registry 中的 `enabled` 不能提升这一边界。

| 信息角色 | 当前实现 | 覆盖 | 门禁 |
|---|---|---|---|
| 当前赛程与结果 | OpenFootball 固定 CC0 文件 | 英超、西甲、德甲、意甲、法甲、英冠；中超当前行未通过权利准入 | 每条 canonical row 保留 provider-scoped fixture/team ID、URL、哈希与双时钟；`date_only` 行隔离。中足联公开接口仅保留 rights-blocked/quarantine 诊断，不能以公开可访问替代商业复用权，也不能作为当前 canonical 或模型输入 |
| 德甲社区赛果/直播交叉核验 | OpenLigaDB ODbL 1.0 `getmatchdata/bl1/<season>` + 显式逐场 `getmatchdata/<matchId>` | 仅德甲且必须与 canonical fixture 唯一精确联结；当前不是完整实时覆盖 | 保留 ODbL 归属、URL、响应哈希、provider match/team ID 和双时钟；拒绝重定向并限制响应大小。逐场结果与进球固定 display/post-match audit，`model_eligible=false`；元数据更新或仅到达开球窗口不能宣称直播；社区源不冒充官方联赛权威 |
| 最近 xG 与状态 | Understat | 五大联赛 | 只取 `as_of` 前已完赛数据，最近 5 场；中超保持缺失 |
| 历史训练比分/赔率 | football-data.co.uk | 五大联赛 | 固定 URL + SHA-256；永不冒充当前数据 |
| 中超历史训练 | OpenFootball CC0 | 2022–2024 | 独立 parser/身份/参数；无赔率保持空值 |
| 当前市场赔率 | 当前 canonical 周期无已获授权的独立市场源 | 保持 unavailable | 不回放旧 ESPN 市场、不把缺失补零；只有体彩官方购买场景或另行核验的高流动性公开市场才能恢复对照 |
| WhoScored 公开卡片对照 | Crawl4AI 执行层候选 | 自动抓取关闭 | 来源权利为 unknown 时在网络前阻断；即使未来获得许可，也必须按 provider-neutral fixture identity 严格联结并先保持 display-only |
| 独立亚洲盘/大小球市场 | 当前无已获书面授权的 OddStorm 数据 | `rights_blocked`，行数 0，`network_opened=false` | 官方条款限制自动抓取、镜像、再分发和转售；页面与逐赔率 history endpoint 在联网和文件轮换前失败关闭。旧归档只保留审计，不回放、展示或入模；只有取得可核验的明确书面授权并由操作者配置授权引用后才能重新评估 |
| 赛事摘要名单证据 | 旧 ESPN 归档仅保留审计 | 当前自动轮询与公开 serving 关闭 | 没有书面授权引用时 `rights_blocked`；不得把旧名单回放成当前首发或球员状态 |
| 体彩公开赛程和赔率 | 中国竞彩网公开 `getMatchCalculatorV1.qry`（`channel=c`） | 页面公开请求；固定 allowlist、无重定向 | 保存原始哈希、玩法和去水概率；源失败隔离，实体低置信时只展示，不进入模型 |
| 体彩字段镜像交叉核验 | Lazq calculator public endpoint | 当前页面可返回部分竞彩市场 | 仅 evidence-only；域名与授权未验证，`purchase_eligible=false`、`model_eligible=false`，不替代体彩官方源 |
| 公开赛前新闻 | BBC Sport / Sky Sports RSS + Google News RSS 发现查询 | 全局/联赛新闻，需按队名与官方来源核验 | 固定 HTTPS allowlist、发布时间与原始哈希；源级失败降级 |
| 英超官方赛程与首发 | Premier League public first-party matchweek/lineups API | 当前未来窗口按有限 matchweek 查询；首发按轮询首次观察归档 | 保留官方 native match ID、原始哈希、`observed_at`；双方首发完整且开赛前观察才 `model_eligible=true`，不做 name-only canonical join |
| 西甲官方赛程与首发 | LaLiga Match Centre 页面 + 官方公开 webview lineup API fallback | 当前未来窗口内发现页面；API 只读取页面公开运行时键并在内存使用 | 页面/API URL 与原始哈希均保留；HTTPS 主机/路径 allowlist、拒绝重定向和大小限制；空或不完整阵容仍 `confirmed=false`、`model_eligible=false`，不写入模型 |
| 意甲官方赛程、场馆与首发 | Lega Serie A 公开 Match Centre + SDP API (`header`/`lineups`) | 当前未来 48 小时窗口按官方主页路由发现；每场最多 bounded API 请求 | 保存 header/lineups 原始哈希和 native match/stadium ID；严格核对官方开球时间与双方球队；场馆名和城市只作字段级 overlay 后进入天气链；无可信发布时间时只用系统 `retrieved_at`，空或不完整阵容不入模 |
| 比赛天气 | Open-Meteo | 仅有可靠场地坐标的比赛 | 精确开球小时；天气作为赛前证据展示，但在没有独立留出验证系数前固定 `enters_model=false`；无坐标不猜测，记录 unavailable |
| 球场坐标 | Open-Meteo Geocoding | 仅使用当前事实源明确提供的 `venue.city/country`，或中超精确官网标签/一手官方登记产生且通过快照校验的字段；OpenFootball 当前行通常没有坐标 | 城市必须精确匹配；解析依据或字段级来源被篡改时拒绝整条当前行；无法匹配保持天气 unavailable，不从队名猜坐标 |
| 伤停与确认首发 | 官方俱乐部/联赛页面优先；SofaScore 候选当前不可用 | 只展示实际取得且可核验的赛前观察 | SofaScore 403/访问阻断保持 unavailable，不用 Crawl4AI 绕过；只有完整 11+11、来源门禁通过且赛前观察的阵容才可形成首发冻结 |
| 比赛事件时间线 | 已归档的明确事件账本（进球、牌、换人、阶段/状态） | 只在来源实际提供事件时展示；按 `effective_at`/`observed_at` 保留时间语义 | 单场 API 做 allowlist 投影并限制条数；事件固定为实时/赛后展示事实，永不进入赛前模型 |
| 实时比赛状态 | 显式启用的 OpenLigaDB ODbL 德甲交叉核验；旧 ESPN `fixture_updates` 只保留归档审计 | 当前只覆盖能精确联结且逐场端点实际产生事件/完场标志的德甲场次，不是全站完整实时比分 | 默认执行仍为无网络的 ESPN 权利阻断；OpenLigaDB 必须 operator 显式选择。`in_progress_window` 不算 live；所有实时/赛后事件固定 `enters_model=false`，不能回填赛前冻结 |
| 实时只读叠加桥 | 独立 `league_platform.live_overlay` 轮询 lane → 原子 `live_overlay.json` → Sites `/api/v1/live-overlay` | 只读取同源 `matchline.live_overlay.v1` 资产；按 `freshness_budget_seconds` 判定 fresh/stale；无资产回退明确 `offline_snapshot`；OpenLigaDB 行必须带 ODbL 归属；本机已构建 server 可选同步 `dist/client/live_overlay.json` | 固定同源路径、schema、时间戳、许可字段和 `policy.model_eligible=false` 均通过才展示；只有行状态 `live/in_progress` 才返回 `live=true`，busy、storage/rights block、无精确连接、无直播或 `in_progress_window` 均为 false；不写 D1 预测、不进入任何赛前冻结；远端仍需显式部署/D1 发布 |
| 球队近期战绩与历史交锋 | D1 `fixtures` + `outcomes` 的已完成赛果；D1 不可用时读取离线包的有界完赛切片 | 当前比赛开赛前的最近 8 场球队战绩、最近 5 次双方交锋 | 只接受有最终赛果且开球时间早于当前比赛的记录；按主客队相对视角计算，不把当前或未来比赛混入预测；离线包缺少逐赛果 `observed_at` 时保持未知，不用快照时间伪造 |
| 球队研究工作台 | Sites `/api/teams?team=`；D1 使用 `fixtures/outcomes/teams/competitions/predictions`，离线时使用同一静态快照投影 | W-D-L、积分/进失球、主客场拆分、休息天数、未来 14 天赛程密度、即将比赛的本队概率与市场差 | 只接受精确规范化球队实体；历史结果按 `kickoff_at <= asOf` 且 `outcome.observed_at <= asOf` 截断；离线静态结果显式 `snapshot_boundary`；未知分数、市场、时间和预测不补零 |
| 球队近期 xG 研究卡片 | D1 `intelligence_observations.kind=team_form_xg`（Understat） | 当前比赛开赛前最近观察的两队 xG 均值、实际进失球和样本量 | 只取 `observed_at < kickoff_at` 的最新可解析记录；保留来源/哈希，缺失或冲突不补零；展示不覆盖冻结特征 |
| 比赛 boxscore 统计 | 旧 ESPN boxscore 仅作历史审计；当前无已授权实时统计源 | 缺失时保持空白 | 不自动回放为当前数据；未来新源仍只保存有界数值投影并固定为赛中/赛后 display-only |
| 球员研究评分 | Sites `stats-utils.ts` 从同一公开 boxscore 白名单指标推导 `research_player_rating_v1` | 赛后快速比较球员的透明摘要，附评分、指标覆盖、版本、输入字段和不可用原因 | 只在分钟数至少 15 且存在表现指标时输出 4.0–10.0 的一位小数；缺分钟/样本不足返回 null；不是 SofaScore/供应商评分，不进入赛前模型，后续必须用独立赛季做校准 |
| 单场研究提醒 | Sites `alert-utils.ts` 从追加赔率、阵容、球员可用性、新闻和 source run 观察推导 | 赔率变化、确认首发、球员状态变化、新闻事实和来源降级；按观察时间排序，供页面轮询和后续通知服务复用 | 只输出有变化或达到失败阈值的有界提醒；保留 `causalState` 和 `modelBoundary`；开赛后、时间异常、新闻和来源健康提醒为展示/审计，不修改冻结预测 |
| 跨比赛研究提醒 | Sites `/api/alerts` 对有界比赛窗口复用 `buildMatchAlerts`；D1 另读最近 `source_runs`，D1 不可用时读取离线来源注册表 | 默认查询未来 20 场、每分钟轮询；支持 `scope`、球队/赛事搜索、`severity`、`kind`、`since`，查询和证据行均有硬上限；全局来源健康不扩大精确比赛筛选 | D1 正常时返回比赛上下文、单场链接、来源链接、提醒因果状态和计数；没有 fixture 观察的来源失败仍以 `fixture=null`、`matchHref=#sources`、`source_degraded`、`display_only` 返回，执行层显式标为 `fetch_runtime`；D1 失败时同样把 SofaScore/ClubElo/FBref/WhoScored/SoFIFA/Crawl4AI 等未通过门禁的来源健康诊断链接到抓取状态，不伪造比赛事实；无效参数仍 400，达到上限时显式 `truncated=true` |
| 批量研究导出 | Sites `/api/v1/matches/export`，复用 `/api/matches` 的有界读模型 | `matchline.match_export.v1` JSON 包，附 `schemaVersion`、`mode`、`asOf`、范围/搜索/筛选（含精确 `competition` 代码与 UTC `date`）、截断状态和研究边界；最多 200 场 | 不暴露原始 D1；离线包明确 `offline_snapshot`，不把缓存当实时；客户必须按 `asOf`、`truncated` 和 `ETag`/`If-None-Match` 解释数据，缺失值不补零；无效联赛/日期参数 400，未变化返回 304 |
| 版本化单场详情 | Sites `/api/v1/matches/:fixtureId`，复用 `/api/matches/:fixtureId` 唯一查询实现 | `matchline.match_detail.v1`：fixture、冻结阶段、概率/市场、来源运行、证据时间线、阵容、球员、天气、赔率和赛后展示统计 | `apiVersion=v1`；旧路由保持兼容；离线详情保留 `offline_snapshot` 边界；无效或不完整 fixture 身份失败关闭，不把赛后事件/统计写回赛前模型 |
| 统一研究服务状态 | Sites `/api/v1/service-status`，D1 读模型失败时使用明确标记的离线证据包 | `matchline.service_status.v1`：模式、新鲜度、事实源分母、Crawl4AI 执行层、存储门禁、前瞻门禁摘要和 `research_only` 边界；支持 `ETag` / `If-None-Match` | `productionReady` 始终为 `false`；Crawl4AI 为 `fetch_runtime` 而非事实源；存储无证明返回 `unknown`，失败周期返回 `blocked`；D1 只允许短缓存，离线包必须 revalidate；不把路由可达性或页面完整度当作公网部署/投注资格 |
| 联赛积分台 | D1 `fixtures` + `outcomes` + `teams` + `competitions`；D1 不可用时读取离线包的近期完赛切片 | D1 按当前 `outcomes` 账本重算积分、胜平负、进失球、净胜球；离线模式只计算有界近期结果并明确 `partial=true` | 只使用开球时间和结果观察时间都不晚于 `asOf` 的最终赛果；结果去重、坏行隔离，查询超限标记 `truncated`；离线模式不会把缺失赛果当作 0 分，也不声称完整官方赛季排名 |
| 单场 JSON 研究报告 | `/api/matches/:fixtureId/report` 的 D1 单场响应；D1 不可用时使用同一 fixture 的 `offline_snapshot` 详情 | 固定导出冻结预测、阶段矩阵、质量层、证据、来源运行和审计边界；事件/比赛统计位于 `displayOnly` | 在线报告顶层 `status=ok`；离线报告顶层 `status=offline_snapshot`，必须保留快照 `asOf`、`missing_not_zero` 和缓存边界；详情不可用时不生成空附件 |
| D1 研究性能 | `evaluations`（顶层）与 `stage_evaluations`（冻结阶段）+ `prediction_stages` + `predictions` + `fixtures` + `competitions` | 分别按赛事、冻结阶段和玩法汇总有效样本、Brier/Log Loss/NLL、Top 5 与市场同样本差值 | 阶段记录必须绑定模型版本 SHA、阶段锁定时间、结果观察时间和赛果指纹；缺失或非法分数隔离，市场缺失不填零；只作展示聚合，不替代严格 walk-forward 与成熟度门禁 |

体彩可售赛程优先进入当前赛程视图：与当前 canonical fixture 在赛事、精确开球和主客队上高置信匹配的比赛复用同一 fixture，保留体彩玩法和原始哈希；
体彩中存在但 canonical 当前源未覆盖的明确联赛比赛会以 `sporttery:<match_id>` 扩展显示。扩展记录的实体匹配置信度
默认仅为 `0.55`，在没有别名/历史高置信消歧前只展示赔率和来源，不进入模型预测；未知联赛、无时间或缺少队名的
记录进入 `lottery_only_errors`，不会被猜测合并。所有体彩返回行另外保留在
`current_data.lottery_sales_schedule`：未知联赛也会显示为 `quarantined`，并带有隔离原因和 `0.0` 实体匹配置信度，
离线/D1 投影同时保留 `provider_fixture_ids["Sports Lottery"]=<match_id>`；因此“可售赛程入口”和“模型可用 fixture”
不会被混为一谈，也不会把体彩展示哈希冒充 provider 原生 ID。

OpenFootball 当前响应保存 URL、来源 ID、CC0 许可标识与内容 SHA-256。中足联等未通过当前权利准入的响应
只保存受限诊断和隔离原因；即使保留官方 tournament/stage/match/team ID、Asia/Shanghai 原始时刻、UTC 开球、
`effective_at/observed_at` 与内容哈希，也不把公开页面可读表述成商业再分发许可。Understat 同时保存压缩传输字节的 `wire_sha256` 与解压内容的
`content_sha256`。任一来源启动失败会记录结构化错误并降级，不会丢弃其他已成功来源。
空赛程、来源错误、联赛覆盖不完整、非 allowlist 主机、非有限数值或不完整比分都会使快照降级、
不可用或直接拒绝，不能仅凭一个新的 `as_of` 冒充 fresh。

当前指针还有一层交接保护：当新一轮 canonical fixture feed 返回 `fixtures=[]` 而上一份 `current.json` 有非空赛程时，
新快照会被完整归档但不会替换当前指针；`data/live/current.publication.json` 保存阻断原因、双方
`as_of`、数量和哈希。前瞻周期对此会失败关闭，避免用旧快照继续生成看似新的一轮预测。只有人工确认
无赛程窗口后，才可使用 `sync_live --allow-empty-sn…371 tokens truncated… `rights_blocked / network_opened=false`，history 归档器也不会创建、
轮换或写入文件。既有原始档案继续追加式保留为历史审计，但当前读模型会过滤 legacy 行，不能把旧赔率重放成
当前市场对照。即使未来取得授权，仍须遵守固定主机/路径、无重定向、响应大小、双时钟和实体严格联结合同，
且不能冒充体彩官方赔率。

同步还会根据有精确开球时间的英超 canonical 赛程选择一个有界的官方 matchweek 窗口，并将官方赛程与首发保存在
`premier_league_official` 源段。官方首发接口没有可解析首发时写入 `status=unavailable`；不会把空响应转成
“全员健康”、预计分钟或替代价值。官方赛程与 canonical fixture 只按赛事和显式 canonical 主客队做唯一匹配；
唯一匹配后可以把官方开球时间、场地和对应字段观察时刻作为 field-level overlay 写入同一 canonical 行，但
OpenFootball fixture ID、行级 source 与 lineage 不改变。Premier League native match ID 只保存在每个覆盖字段的
provenance 和官方源段中，不能冒充 OpenFootball native ID；歧义、坏时间或球队不一致时不覆盖。

快照校验层会再次要求任何 `model_eligible=true` 的官方首发同时满足 `confirmed=true`、引用同一官方
fixture，并且其 `source.retrieved_at` 严格早于开球；解析器或快照被篡改时直接拒绝，不会把赛后首发带入模型。

当官方赛程与 canonical 比赛在开球时间、主队和客队三项上唯一严格匹配时，当前快照会把官方首发挂到该比赛的
`current_features.official_lineup`；若只有空响应或存在重复候选，则保持缺失/冲突状态，不覆盖成“无球员”。
只有官方首发完整或确实带球员记录时，未来预测的球员层才优先显示该一方；预计分钟和替代价值仍明确为缺失，
不会因首发确认而假装完成球员影响建模。

意甲官方链路使用 Lega Serie A 主页公开的比赛路由发现当前赛季 native match ID，再调用其公开 SDP
`header` 与 `lineups` 端点。端点主机、路径、响应大小和重定向均有 allowlist；header 的官方开球时间、
match ID、双方队名必须与 canonical fixture 严格一致。API 返回的 `fielded`/`benched` 仅在双方各 11 名
starter 且系统观察早于开球时标记 `confirmed=true`，否则保留为 display/audit-only，不把空数组解释成
“没有伤停”或完整阵容。由于公开 payload 没有可复核的首发发布时间，`apiCallRequestTime` 不冒充
`effective_at`，模型时间语义仍以本次实际 `retrieved_at` 为准。header 中非空的 `stadiumName`、
`stadiumId` 和 `cityName` 会在同一严格球队联结后作为字段级场馆证据；canonical row identity 不变，城市和国家
再经精确 Open-Meteo 地理编码后生成天气。没有城市、联结歧义或来源校验失败时不从球队主场猜测。

严格报告更新后，离线审计包使用同一份当前快照和报告重新生成：

```bash
/usr/bin/python3 -m league_platform.build_offline_bundle \
  --compact-output matchline_sites/public/offline_snapshot.json
```

`league_platform/site/offline_data.js` 仍是旧站点和审计回放使用的外置软链接；Sites 额外读取
`matchline_sites/public/offline_snapshot.json` 这份有界副本。它只在 D1 未绑定或读取失败时作为
明确标记为 `offline_snapshot` 的只读降级，不改变实时 API、模型门禁或数据缺失口径。

同步会读取 OpenFootball 固定 CC0 文件并保存全部可审计行，但当前读模型只投影最近 7 天赛果和未来 7 天精确开球赛程；
每次同步还会将完整快照追加到 `data/live/archive/`，原始内容按 SHA-256 去重保存，支持回放赔率/新闻/天气/阵容
在当时截点的覆盖变化。新闻、天气、伤停与首发属于可选源，短暂不可用不会掩盖核心赛程源状态，也不会被伪造为完整覆盖。

## 未来预测契约

`GET /api/v1/predictions` 是公开预测接口，只从当前 `prospective-model-lock-current.json`
对应的 `data/live/prospective_predictions.jsonl` 读取无冲突冻结记录；它不再把单次当前快照
即时计算的研究草稿作为正式预测返回。每条记录必须同时满足：`kickoff_at > as_of`、冻结 cutoff
不晚于 `as_of`、预测观察时间和归档时间不晚于 `as_of`、模型锁哈希一致、概率契约合法且没有冲突。
尚未写入合法冻结记录的比赛进入 `blocked[].reason=no_causal_freeze_record`，页面显示等待冻结，
不会以较新的即时数据回填。旧的即时研究计算仍可由 Python `PlatformStore.predictions()` 用于
诊断，但不属于 Sites/API 的公开预测源。

研究者需要查看未进入当前前瞻锁的 as-of 草稿时，使用显式的
`GET /api/v1/research-predictions`。该端点返回同一份公开锁定结果之外的
`research_predictions` 投影，并保留 `research_only`、覆盖等级和缺失字段；它不会改变
`/api/v1/predictions` 的生产边界，也不会把草稿写入模型锁或 D1 预测表。
其响应 schema 为 `matchline.research_predictions.v1`，沿用 matches 的
`scope`、`horizon`、`competition`、`date`、`q`、`limit` 和 opaque `cursor` 查询；
`items`/`fixtures` 中每一行都保留 fixture identity 及
`researchPredictionState`（`available`、`blocked`、`unavailable`、`not_eligible`、
`not_applicable`）、reason/message。`prediction` 始终为 `null`，研究草稿只允许
非数值覆盖审计字段，正式概率和市场字段不得出现在该端点。

公开接口在上述时间截断后，才返回满足 `kickoff_at > as_of` 的比赛，并返回：

- provider-neutral canonical fixture ID、真实来源身份与精确开赛时间；
- `as_of` 和历史 `training_cutoff`；
- `history_context`（`matchline.history_context.v1`）：与该预测使用同一份 verified OpenFootball 历史账本，按训练截止时间保留双方最近 8 场、W-D-L、进失球、积分/场、零封/哑火和逐场身份；该摘要只作研究解释，不会把训练截止后的赛果回填到预测；
- Elo 与 Dixon-Coles 两条概率，不隐藏候选差异；
- 胜平负、让球、总进球 0/1/2/3/4/5+、完整比分矩阵及 Top 5、半全场联合概率；
- 大小球概率同时返回明确的 `total_over_under_line`（当前模型为 `2.5`）；旧归档没有结算线时必须保留 `null`，页面显示“结算线未记录”，不能从概率键推断盘口；
- 市场隐含概率、模型差值、覆盖等级、缺失字段、冲突标记、模型版本和冻结时间；
- 参与该场的 provider 列表和当前特征覆盖；
- 对升班马/新球队的历史样本不足阻断；
- 当前赔率、伤停、首发缺失门禁。
- `player_layer` 保留球员、位置、状态和可验证的预计分钟；替代价值缺少独立验证时明确为
  `null`/不可用，且 `model_eligible=false`，不会静默填零或进入最终概率。
- 单场比赛的 `evidence.events` 是从通用情报账本派生的有界时间线，不是新的训练表；每条事件保留来源、原始观察时间、
  冲突组和 `displayOnlyReason=live_or_postmatch_event`。即使原始观察误带 `entersModel=true`，API 也不会把事件标记为
  赛前特征。球员可用性在页面结构化展示预计分钟、替代价值和置信度，缺失仍保持 `null`/“未知”。
- `evidence.lineups` 优先读取结构化 `lineup_snapshots`；结构化投影尚未回填时，才从同一 fixture 的 `lineup`/
  `roster_evidence` 原始观察生成有界展示行，并保留来源哈希、`observed_at`、`effective_at` 与冲突组。该回退不改变
  `confirmed`/`enters_model`，不把普通名单升级为首发，也不把展示数据送入模型。
- `/api/ingest` 对缺失 canonical ID 的观察只通过已有 `entity_aliases` 做 provider-aware 解析：要求 `resolution_state=resolved`、
  置信度至少 `0.95`、provider 前缀一致且唯一；未匹配、低置信度或冲突记录仍以未关联原始观察追加保存，不用队名或相似字符串猜测实体。

每场还返回四个不可变冻结槽位（`t_minus_24h`、`t_minus_6h`、`t_minus_90m`、
`lineup_confirmation`）及稳定 `version_id`。跨过 90 分钟本身不会伪造首发确认：只有
明确 `confirmed=true` 且带有不晚于该 `as_of` 的首发观察时间，才会把当前阶段标为
`lineup_confirmation`；否则保留为 `t_minus_90m`。尚未达到的阶段保持 `pending`，不会复制早期概率。

每个冻结槽位还必须满足 `freeze_cutoff_at >= evaluation_window_started_at`。这条约束独立于“比赛尚未开赛”
和“fixture 是否已在 cutoff 前观察”：若当前模型版本是在某个 T−24/T−6/T−90/首发 cutoff 之后才锁定，
该槽位必须返回 `freeze_cutoff_before_evaluation_window`，不能用新模型向过去回填。严格 builder 与不可变归档
各自执行一次该门禁，避免调用方遗漏检查；旧锁下已经追加但未评分的行只保留为 legacy audit，不参与新窗口评分。

`GET /api/v1/snapshot` 的 `prospective_evaluation.blocked_diagnostics.next_freezes` 只读暴露
当前周期已经从校验过的赛程快照推导出的下一次冻结机会（联赛、fixture、开球时间、阶段和截止时间）。
它是运营提示和审计证据，不是模型特征；缺少完整时间字段的诊断项不会进入该列表，页面也不会把阻断
转换为零概率或强结论。

Sites 首页的 `/api/prospective-status` 将同一组字段投影成研究者可轮询的只读门禁面板：当前前瞻状态、
已评分/待评分/结果冲突、下一批冻结目标、模型版本短哈希和发布阻断原因。D1 未绑定时响应明确为
`mode=offline_snapshot`，缓存最多 60 秒，`productionAllowed=false`；接口和页面都不会把下一次冻结
当成已经生成的预测，也不会把“读取成功”解释成生产门禁通过。

严格历史报告中的 `freeze_stages` 还会把 24 小时、6 小时、90 分钟冻结事件放回真实时间轴：
同一时间先生成预测，再批量更新赛果。历史源没有官方首发 `observed_at/effective_at` 时，
`lineup_confirmation` 为 `unavailable` 并阻断该阶段门禁；这不影响前三个阶段的时间泄漏审计，
也不会把开赛前最终快照冒充更早版本。

当前输出属于 `research_only`。每场比赛分别披露市场、天气、新闻和 SofaScore 覆盖；模型只有在
伤停/首发完整时才会把对应字段标为 available，但这些新增特征尚未通过独立留出回测，不能解除生产门禁。
任何情况都不能形成投注金额或收益建议。

展示层同样执行覆盖门禁：`coverage.level=low` 或关键情报冲突时只显示“预测不展示”、缺失字段和冻结时间线，
不渲染模型概率条；`medium` 只能以“研究对照”显示并附带缺失字段，不能被解释为投注优势。

研究者仍可在单场详情看到独立的 `matchline.historical_baseline.v1` 历史先验投影：它只接受
`verified_openfootball` 历史模型、双队历史样本达标且市场、xG、天气、伤停、首发等字段明确为未入模的行。
该投影与正式预测字段分离，即使正式研究预测因低覆盖而隐藏概率，也不会解除隐藏或改变发布门禁；它只用于历史比较，
明确标注训练截止、观测时间和样本量，不代表赛前预测、投注优势或体彩购买建议。来源缺失、混源或概率不合法时返回空，
不会用旧模板补齐。

来源账本的冲突解析只把来源优先级最高的记录作为审计展示首选，不会把它当成冲突已经解决；
同一字段存在不同 payload 时，`resolve_observations` 将 `usable_for_model` 置为 `false`，
包括市场等非关键字段，直到后续观察完成一致性确认。

情报账本追加由 sibling lock 串行化，并在释放锁前 flush/fsync，避免多个抓取进程把 JSONL 行交错写坏。
读取默认保留合法观测并把坏行记录到 `last_read_errors`；需要发布或门禁的调用可以使用严格读取，遇到坏行直接阻断。
坏历史行不会被覆盖或静默删除，修复只能追加来自不可变原始快照的完整观测并保留修复证据。
每轮运行证据会流式扫描账本并记录物理行数、有效 JSON 行数、坏行号、错误摘要和 SHA-256；诊断信息只用于审计，
不把坏行当作模型输入，也不因默认读取的容错行为而宣称账本完全无错误。

审计台还可读取生成的严格回测附件 `strict_model_health`。它只在报告的各联赛历史截止时间与当前
历史账本一致、且嵌入的 `prospective_lock` 与当前锁的身份字段完全一致时附加到 API/离线快照；报告缺失、
截止时间不一致或锁身份过期时，系统保留基础模型摘要并返回 `strict_backtest.status=stale`，不宣称严格报告
已同步。`/api/v1/model-evaluations` 与 `/api/v1/snapshot` 使用同一套截止时间和锁身份匹配规则。严格报告的市场比较
使用同一批有市场概率的留出比赛，避免把全样本模型分数与市场子样本混比。
