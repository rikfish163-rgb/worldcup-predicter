# 六联赛赛前公开信息源调研

调研日期：2026-08-11（Asia/Shanghai）  
范围：英超（EPL）、西甲（LaLiga）、德甲（Bundesliga）、意甲（Serie A）、法甲（Ligue 1）、中超（CSL）；赛程/结果、伤停、确认首发、新闻/发布会、天气、赔率/盘口、xG/状态、积分/战意。  
目标：为赛前预测平台建立可审计的来源组合，不把“网页能打开”“接口返回预测首发”或“搜索到一条新闻”误记为生产级数据覆盖。

## 先给结论

没有一个免费、公开、稳定且许可清晰的来源同时覆盖六个联赛的全部字段。建议采用分层组合：

1. **结构化比赛主源**：先用注册后的 [football-data.org v4](https://www.football-data.org/documentation/quickstart) 做低成本基线；需要伤停、首发、赔率和更深统计时，以 [API-Football](https://www.api-football.com/documentation) 或 [Sportmonks](https://www.sportmonks.com/football-api/) 的授权套餐做生产候选。六联赛的每个 `league-season-field` 必须在上线前逐项检查 `coverage`，不能仅凭供应商的总联赛数推断中超字段完整。
2. **官方事实层**：各联赛官网、CFA 官网和俱乐部官方新闻/发布会是赛程变更、教练表态、伤停及官方确认首发的事实核验层；这些网页多数是动态 HTML，不应假设存在可长期依赖的公开 JSON API。
3. **公开降级层**：德甲用 [OpenLigaDB](https://www.openligadb.de/) 作为无密钥赛果补源；中超历史用 [openfootball/world](https://github.com/openfootball/world) 的 CC0 文件；五大联赛 xG/状态可用 [Understat](https://understat.com/) 做研究辅助，但不覆盖中超，也不应假定其隐藏接口永久不变。
4. **天气**：主用 [Open-Meteo](https://open-meteo.com/en/docs)，以 [MET Norway Locationforecast](https://api.met.no/weatherapi/locationforecast/2.0/documentation) 作为全球天气的第二源。保存球场坐标、预报发布时间、模型/更新时间和抓取时刻。
5. **赔率/盘口**：优先授权 API（API-Football、Sportmonks 或 [The Odds API](https://the-odds-api.com/liveapi/guides/v4/)）；中国竞彩 [sporttery.cn](https://www.sporttery.cn/) 仅作为依法授权后的中超候选。当前网络对其公开接口返回 567，不能通过换 UA、代理、验证码或 WAF 规避来“修复”。

所有来源都应保存 `provider`、请求 URL、`retrieved_at`、数据内的有效时间/发布时间、HTTP 状态、原始响应哈希和解析器版本。预测时只使用 `effective_at <= as_of` 的观察；“预计首发”必须和“已确认首发”分开建模。

## 可访问性实测（本机、只读）

以下是 2026-08-11 从本机发出的普通 HTTPS GET 结果。状态码只说明本网络、该时间和该 URL 的观察结果，不代表供应商全球 SLA。没有使用登录凭据，没有绕过验证码、WAF、访问控制或 robots 限制。

| 来源/示例 URL | 观察结果 | 解释 |
|---|---:|---|
| [football-data.org coverage](https://www.football-data.org/coverage) | 200 HTML | 覆盖页可读；[API `/v4/competitions`](https://api.football-data.org/v4/competitions) 无密钥也返回目录。比赛资源无密钥返回 403。 |
| [OpenLigaDB `/getmatchdata/bl1/2025`](https://www.openligadb.de/api/getmatchdata/bl1/2025) | 200 JSON | 无认证可读，实测返回德甲整季比赛对象。 |
| [Open-Meteo forecast](https://api.open-meteo.com/v1/forecast?latitude=51.555&longitude=-0.279&hourly=temperature_2m&forecast_days=1) | 200 JSON | 无密钥可读。 |
| [MET Norway compact forecast](https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51.5&lon=-0.28) | 200 JSON | 使用带应用标识的 User-Agent 后可读。 |
| [Google News RSS 查询示例](https://news.google.com/rss/search?q=Premier+League+injury&hl=en-US&gl=US&ceid=US:en) | 200 XML | 可作为新闻发现层，不等于原文许可或官方确认。 |
| [GDELT DOC API](https://api.gdeltproject.org/api/v2/doc/doc?query=%22Premier%20League%22&mode=artlist&maxrecords=5&timespan=1d&sort=datedesc&format=json) | 429 | 返回“每 5 秒最多一次”的提示；应遵守其节流，不密集重试。 |
| [英超官方赛程](https://www.premierleague.com/en/matches) | 200 HTML | 官方动态页面可读；没有据此推断稳定 API。 |
| [LaLiga 官方日历](https://www.laliga.com/en-FR/laliga-easports/calendar) | 200 HTML | 官方日历可读。 |
| [Bundesliga 官方比赛日](https://www.bundesliga.com/en/bundesliga/matchday) | 200 HTML | 官方动态页面可读，响应带压缩。 |
| [Lega Serie A 官方赛程结果](https://www.legaseriea.it/serie-a/calendario-risultati) | 200 HTML | 官方页面可读。 |
| [Ligue 1 官方日历](https://ligue1.com/en/calendar/ligue1) | 200 HTML | 官方页面可读。 |
| [中国足协赛事目录](https://www.thecfa.cn/Competitions/index.html) | 200 HTML | 官方赛事目录可读；[中超专题页](https://www.thecfa.cn/CFAsuper/index.html) 当前返回“页面升级维护中”。 |
| ESPN 六个 `scoreboard` 端点（`eng.1/esp.1/ger.1/ita.1/fra.1/chn.1`） | 均 403 | 本机当前被 Access Denied；不可通过伪装、轮换 UA 或代理绕过。项目曾有历史成功记录，但不能替代当前验收。 |
| [Understat 首页/联赛页](https://understat.com/league/EPL/2025) | 200 HTML | 页面可读；项目现用的 `getLeagueData` 示例在本次复测返回 404，隐藏接口兼容性不足。 |
| FBref 五个联赛统计页 | 均 403 | 返回 Cloudflare challenge；不做挑战绕过。 |
| SofaScore `/api/v1/sport/football/scheduled-events/2026-08-11` | 403 JSON | 当前网络被拒；不把未授权/受保护接口当生产依赖。 |
| 体彩 `webapi.sporttery.cn/.../getMatchCalculatorV1.qry` | 567 | WAF/边缘拦截；不尝试“移动 UA bypass”、验证码或代理绕过。 |
| API-Football 无 `x-apisports-key` | 403 JSON | 公开主机可达，但 API 需要应用密钥。 |
| Sportmonks 无 `api_token` | 401 JSON | 公开主机可达，但 API 需要令牌。 |
| The Odds API `/v4/sports` 无 key | 401 JSON | 公开主机可达，但需要 API key。 |
| [openfootball/world 2024 中超文件](https://raw.githubusercontent.com/openfootball/world/master/asia/china/2024_cn1.txt) | 200 text | 可直接读取 CC0 历史文件；文件名 `_cn1` 的头部实际标为 `China | Super League`。 |

## 角色与字段总览

符号：`✓` 有结构化覆盖；`△` 有限、需按联赛/赛季核验或仅网页；`—` 不提供；`R` 研究/抓取风险，不宜作为生产主源。

| 来源 | EPL | LaLiga | Bundesliga | Serie A | Ligue 1 | CSL | 主要字段 |
|---|---:|---:|---:|---:|---:|---:|---|
| football-data.org v4 | ✓ | ✓ | ✓ | ✓ | ✓ | △ | 赛程、赛果、积分、球队、部分阵容/事件；深度字段取决于套餐；无 xG、伤停和天气 |
| API-Football | ✓ | ✓ | ✓ | ✓ | ✓ | △ | 赛程、赛果、积分、事件、统计、伤停/停赛、首发、赔率；各 `coverage` 可能为空 |
| Sportmonks | ✓ | ✓ | ✓ | ✓ | ✓ | △ | 赛程、事件、积分、阵容/伤停、统计、xG/赔率/新闻 add-on；付费选择联赛 |
| 官方联赛/CFA/俱乐部页面 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 官方赛程变更、积分、公告、新闻/发布会、部分伤停/确认 XI；动态 HTML，非统一 API |
| ESPN scoreboard/summary | △ | △ | △ | △ | △ | △ | 事件 ID、球队 ID、开赛时间、状态、比分；summary 可能含赔率/阵容/新闻片段；当前本机 403 |
| OpenLigaDB | — | — | ✓ | — | — | — | 德甲赛程、赛果、球队等；社区源、无伤停/xG/赔率 |
| OpenFootball/world | △ | △ | △ | △ | △ | ✓ | 静态历史赛程/赛果；CC0；非实时、无其他赛前字段 |
| Understat | ✓ | ✓ | ✓ | ✓ | ✓ | — | 比赛/球队/球员 xG、xGA、npxG 等；页面可读，接口非稳定；无中超 |
| FBref | ✓ | ✓ | ✓ | ✓ | ✓ | — | 结果、积分、球员/球队高级统计（含 xG 列视页面）；当前 Cloudflare 403 |
| StatsBomb Open Data | △ | △ | △ | △ | △ | — | 选定历史比赛事件、射门 xG、lineups、部分 360；定制研究许可，不是六联赛实时源 |
| Open-Meteo / MET Norway | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 经纬度天气预报/部分历史或模型时序；与联赛无关，需由球场坐标关联 |
| API-Football/Sportmonks/The Odds API | ✓ | ✓ | ✓ | ✓ | ✓ | △ | 1X2、让球/点差、大小球及多博彩公司；key、套餐和地域许可必查 |
| GDELT/Google News RSS | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 新闻标题、来源、链接、时间、摘要/语言；仅发现层，原文版权仍归发布者 |

## 结构化 API 与比赛主源

### 1. football-data.org v4：低成本主基线

- URL：[coverage](https://www.football-data.org/coverage)、[pricing](https://www.football-data.org/pricing)、[v4 文档](https://docs.football-data.org/general/v4/index.html)。目录中的联赛代码包括 `PL`、`PD`、`BL1`、`SA`、`FL1`，查找表中也有 `CSL`/2044；但免费层页面明确列出的核心免费联赛是五大联赛，CSL 的当前套餐与字段权限必须在注册后用真实请求确认。
- 字段：`/competitions/{id}/matches` 的赛程、开球时间、状态、比分、主客队、场地等；`standings` 积分；深度套餐可含 line-ups/subs、进球、牌、阵容。没有官方的 xG、伤停、天气或新闻字段；赔率是独立 add-on 而非默认免费数据。
- 频率与新鲜度：免费套餐 10 calls/min；免费层比分/赛程有延迟；官方政策也给出未认证客户端仅能访问目录且 100 requests/24h。不要逐场高频轮询，按日期/联赛批量拉取并缓存。
- 许可风险：必须注册并保密 token；条款要求可见署名“Football data provided by the Football-Data.org API”、一个 key 对应一个应用；服务取消后不再允许继续引用通过 API 获得的数据；徽标/照片另有权利人。免费可读不等于可把原始数据再次分发。
- 选择：适合作为赛事/积分基线；上线前逐个联赛做赛季、延迟、lineup 和 CSL 覆盖验收。失败时降级到官方联赛页或授权 API，历史回测使用已核验的本地快照，不把旧快照标记为当前。

### 2. API-Football（API-SPORTS）：一站式字段候选

- URL：[coverage](https://www.api-football.com/coverage)、[文档](https://www.api-football.com/documentation)、[pricing](https://www.api-football.com/pricing)。供应商列出 1,200+ 联赛/杯赛及 fixtures、standings、events、lineups、injuries、sidelined、statistics、pre-match/in-play odds；覆盖页明确提醒字段可随赛季或场次变化。
- 字段：赛前可拉 fixture、球队/联赛 ID、积分、近况、伤停/停赛；lineups 需单场 fixture 查询，confirmed XI 以供应商返回的 start-XI/首发数组及发布时间为准（不要把字段名硬编码成跨供应商统一名）；statistics 可含射门、控球、牌等，xG 是否有值必须检查响应，不可把空值补成 0；赔率包含 bookmaker/market/selection/price/更新时间。
- 频率与新鲜度：免费层 100 requests/day、10 requests/min；官方文档示例说明 statistics live 时约每分钟更新、非进行中场次可每日一次，injuries endpoint 约每 4 小时更新且建议每日调用。赔率和 lineup 更新是 provider/competition-specific，应读取响应和时间戳。
- 许可风险：需要 key；条款禁止转售其数据、禁止多账号堆叠免费额度，并提示上游联赛/球队标识可能另有权利。生产公开产品需确认所购计划和再分发权。
- 选择：最适合做受控试点；先选一场每个联赛，验证赛前 24h、6h、1h 和 kickoff-75min 的伤停/lineup/odds 覆盖，再决定套餐。key 缺失或 403 时只降级为已验证的比分/积分源，不重试无效请求。

### 3. Sportmonks：更深的付费备选

- URL：[Football API](https://www.sportmonks.com/football-api/)、[plans/pricing](https://www.sportmonks.com/football-api/plans-pricing/)、[lineups 文档](https://docs.sportmonks.com/v3/tutorials-and-guides/tutorials/includes/lineups)。公开资料称覆盖 2,200+ 联赛；Starter/Growth/Pro 允许选择 5/30/120 个联赛，Enterprise 才是全量候选。免费计划主要用于 Danish Superliga/Scottish Premiership，不应当作六联赛免费方案。
- 字段：fixtures、live scores、events、standings、team/player stats；`include=lineups` 提供首发和替补，`sidelined.sideline` 可列伤停/停赛；xG、odds、news 属 add-on/套餐能力，需按联赛 coverage 验证。`expectedLineups` 是预估首发，绝不能标成 confirmed XI。
- 频率与新鲜度：官方计划按实体/小时计费（页面列出约 2,000–5,000 calls/entity/hour，具体以账号套餐为准），响应带 rate-limit 信息；批量 `include`、缓存球队/球员字典，避免逐场 N+1。
- 许可风险：商业授权、非开放数据；套餐、历史数据、赔率和新闻权利分别确认。无 token 的公开主机请求返回 401。
- 选择：当 API-Football 的中超 lineup/odds/xG 覆盖不足时做第二商业候选。若账号或套餐失效，降级到 API-Football + 官方网页，并将缺失字段置空。

### 4. ESPN public scoreboard/summary：项目已有适配，但不是已证实的生产源

- URL 模式：[scoreboard](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard)、[summary](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=EVENT_ID)；项目当前代码映射 `eng.1`、`esp.1`、`ger.1`、`ita.1`、`fra.1`、`chn.1`。
- 字段：scoreboard 通常给原生 event/competition/team ID、开赛时间、状态、比分、主客队名称；summary 可能含 commentary、leaders、roster/lineups、injury notes 和 `pickcenter` 的 bookmaker moneyline，但字段随事件和地区变化；不要假定所有六联赛都有 odds、阵容或 xG。
- 频率/许可：没有公开稳定的 API SLA 或明确的再分发许可；遵守 ESPN/相关站点条款和低频缓存。每个响应应保存原始 JSON 哈希、`retrieved_at` 和 provider 名称。
- 实测：本机六个 scoreboard 均 403 Access Denied；项目既有历史验收不等于当前环境可用。禁止用 UA 伪装、代理轮换、验证码/WAF 绕过。失败时直接转 football-data.org/API-Football/官方页面，不用旧 ESPN 快照伪装 fresh。

## 官方事实源：赛程、伤停、发布会和确认首发

### 联赛/协会页面

| 联赛 | 官方入口 | 可用字段 | 当前判断 |
|---|---|---|---|
| 英超 | [matches](https://www.premierleague.com/en/matches)、[首页新闻](https://www.premierleague.com/) | 赛程、结果、部分直播状态、公告/新闻、积分入口 | 本机 200 HTML；动态页面，无承诺公开 API。 |
| 西甲 | [calendar](https://www.laliga.com/en-FR/laliga-easports/calendar)、[官方首页](https://www.laliga.com/en-GB) | 日历、开球时间、结果、积分、统计、新闻/公告 | 本机 200 HTML；页面明确提供 season/calendar/results/standings/news。 |
| 德甲 | [matchday](https://www.bundesliga.com/en/bundesliga/matchday)、[官方产品 fixtures](https://products.bundesliga.com/fixtures) | 比赛日赛程/结果、积分、官方赛前/赛后内容 | 本机 200 HTML；产品页偏媒体/商业 feed，不假定可免费再分发。 |
| 意甲 | [calendar/results](https://www.legaseriea.it/serie-a/calendario-risultati)、[官方首页](https://www.legaseriea.it/serie-a) | 赛程、结果、积分、公告、matchday insights | 本机 200 HTML；动态数据和 PDF 需保留来源版本。 |
| 法甲 | [calendar](https://ligue1.com/en/calendar/ligue1)、[文章/新闻](https://ligue1.com/en/) | 赛程、结果、新闻、官方公告和视频 | 本机 200 HTML；路由曾变化，需用 allowlist 和健康检查。 |
| 中超 | [CFA competitions](https://www.thecfa.cn/Competitions/index.html)、[CFA 官方](https://www.thecfa.cn/)、[中超专题](https://www.thecfa.cn/CFAsuper/index.html) | 竞赛日历、纪律/裁判/公告、部分赛事新闻与视频 | 赛事目录本机 200；中超专题当前维护页 200。赛程/积分若无专页，需使用授权 API/俱乐部页补全。 |

这些页面的共同限制：HTML 内容可读不等于允许批量抓取或再分发；没有统一字段名、没有稳定的历史版本 API，也没有统一的伤停/首发时间戳。适合保存“官方 URL + 标题 + 发布时间 + 摘要/声明原文链接”，不适合把网页 DOM 当永久契约。

### 俱乐部官网、官方发布会和确认 XI

- 伤停的最高证据等级是俱乐部官方公告、主教练赛前发布会或联赛官方纪律公告；二手报道只能作为 `reported`，不能直接生成 `out`。
- 确认首发通常在开赛前约 60–75 分钟由联赛 match centre、俱乐部官方账号或授权数据商发布。保存发布时刻和“首发/替补/未入选”语义；`expectedLineups`、媒体预测和 fantasy probable XI 一律另存为预测字段。
- 六联赛没有一个公开、统一、免费且许可清晰的伤停 API。生产需要 API-Football/Sportmonks 等授权供应商，或人工审核的官方来源 allowlist；遇到缺失就将 `injuries_available=false`/`confirmed_lineups_available=false`，不要用最近一次阵容或球队 Elo 代替。
- 社交平台原生 API 常需登录/应用审核；不绕过访问控制，不把搜索结果缓存成“官方确认”。

## xG、状态、积分和战意

### Understat：五大联赛研究辅助

- URL：[league pages](https://understat.com/league/EPL/2025)（同样有 La_liga、Bundesliga、Serie_A、Ligue_1）。页面公开展示球队/球员 xG、xGA、npxG、射门等，可聚合近 5 场 xG-for/xG-against 作为状态特征。
- 覆盖：五大联赛，不含中超；不提供官方伤停/首发/天气/赔率。使用时仅取比赛开球时间早于 `as_of` 的已完赛场次。
- 可访问性：本机首页和联赛页 200，但项目适配使用的 `getLeagueData/{league}/{season}` 路径当前复测 404；页面结构和隐藏请求没有公开稳定 SLA。应把解析器健康检查、响应哈希、空值率和字段版本纳入门禁。
- 许可/频率：网站数据和页面版权归站方/上游，未找到可把抓取结果任意再分发的开放许可证；无公开稳定限流表。低频、缓存、遵守站点条款；被拒时不要改 UA/绕过 WAF。
- 降级：五大联赛用授权统计 API 或 StatsBomb/FBref 的已获许可历史数据；中超保持 xG 缺失，用独立联赛 Elo/比分状态，不把 Elo 重命名为 xG。

### FBref 与 StatsBomb Open Data：历史/研究用，不做实时主源

- [FBref 比赛/联赛统计](https://fbref.com/en/comps/9/Premier-League-Stats) 提供赛果、球队/球员高级统计和页面内 xG 列（具体提供者随表格变化），覆盖五大联赛；本机五个联赛页均被 Cloudflare challenge 403，不能做当前环境的可用性承诺。
- [StatsBomb Open Data](https://github.com/statsbomb/open-data) 提供选定比赛的 competitions、matches、events、lineups 和部分 360；射门事件包含 xG 等字段，但不是六联赛全量或实时 feed。其仓库说明要求研究/分析中注明 StatsBomb 并使用 logo，定制许可不应假定允许商业产品或二次分发。
- 两者均不能提供中超完整实时伤停/首发/赔率。失败时使用授权 API；历史研究数据保留原始文件、许可文本和版本 SHA。

### 积分与“战意”不要混为一项外部字段

- 积分、排名、净胜球、近况：优先结构化积分源或官方页面，带 `standings_as_of`。
- 战意应由可审计规则派生：距冠军/欧战/保级线的积分差、剩余比赛数、官方赛制与晋级条件、休息天数、杯赛/洲际赛冲突、教练或俱乐部明确表态。每个信号标记来源和时间。
- 不使用赛后积分、赛后新闻或最终盘口回填到赛前特征；跨联赛直接复制“战意阈值”会造成泄漏和制度偏差。

## 赔率、盘口和市场信息

### 授权/商业候选

1. **API-Football**：有 pre-match/in-play odds；可返回 bookmaker、market、selection、price 和更新时间。按 fixture/market 拉取并记录 provider，不把“有赔率”当作合规授权；免费 100/day 仅适合小样本验证。
2. **Sportmonks**：公开产品页列出 50+ bookmakers、150+ markets 的 odds add-on；需商业套餐，按实体/小时限流。
3. **The Odds API**：[v4 文档](https://the-odds-api.com/liveapi/guides/v4/) 要 key，当前主市场可含 `h2h`、`spreads`、`totals`，请求成本按 regions × markets 计，响应有 `x-requests-*` 配额头；常见 sport keys 覆盖五大联赛，CSL 不应假定存在，必须从 `/v4/sports` 目录逐日检查。免费/商业额度和可再分发权以账号条款为准。
4. **football-data.org Odds Add-on**：pricing 页列出 pre-match Home/Draw/Away odds；适合 1X2 基线，但不等于亚洲让球全市场，也不覆盖所有免费层联赛。

### 中国竞彩与公开网页

- [体彩官方入口](https://www.sporttery.cn/) 的历史项目接口 `https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu` 返回结构中曾包含：比赛编号/联赛/主客队/时间、`had`（胜平负）、`hhad`（让球胜平负）、`ttg`（总进球）、`crs`（比分）、`hafu`（半全场）及更新时间。
- 本机当前访问该接口返回 567 WAF。禁止用移动 UA、代理、验证码、IP 轮换或其他访问控制规避。只有得到官方/合法授权并确认适用法律后，才可接入；否则降级到授权 odds API 或空值，不用旧 odds 冒充当前。
- 赔率应保存 bookmaker、市场、selection、原始价格、抓取时间、是否开盘/封盘和去水方法；模型概率、市场隐含概率和投注建议是不同层，本文不把任何来源当作收益保证。

### 历史赔率

- [football-data.co.uk data](https://www.football-data.co.uk/data.php) 的五大联赛赛季 CSV 本机可读，示例 `https://www.football-data.co.uk/mmz4281/2526/E0.csv` 返回结果、射门/角球/牌（视赛季）和多家 bookmaker 1X2 odds；不覆盖 CSL，也不是实时盘口。
- 其网站说明免费访问，但未提供适合本项目直接再分发原始 CSV 的明确开放数据许可证。只保存 URL、SHA-256 和本地研究快照，商业/公开分发前取得书面确认；无赔率就保持空值。

## 天气

### Open-Meteo（主源）

- URL：[forecast docs](https://open-meteo.com/en/docs)、[pricing/limits](https://open-meteo.com/en/pricing)。按球场纬度/经度请求 `temperature_2m`、`relative_humidity_2m`、`precipitation_probability`、`precipitation`、`wind_speed_10m`、云量和气压等；可用 7 天默认、最多 16 天 forecast 参数，历史/历史预报产品另查权限。
- 免费公共 endpoint 适合非商业用途；官方 pricing 页面给出约 600 requests/min、5,000/hour、10,000/day、300,000/month 的限制且无 uptime guarantee。底层开放天气资料要求 CC BY 4.0 署名；商业服务应购买 commercial API/licence。
- 每个快照保存 `latitude/longitude`、timezone、`generationtime_ms`、模型更新时间、当地 kickoff 对应小时及 `retrieved_at`。如果比赛超过 forecast horizon，不提前填入伪精确天气。
- 失败降级到 MET Norway；两者都失败时保留天气缺失，不用城市平均值代替赛时观测。

### MET Norway（第二天气源）

- URL：[Locationforecast docs](https://api.met.no/weatherapi/locationforecast/2.0/documentation)、[terms](https://api.met.no/doc/TermsOfService)。全球点预报 JSON 可给温度、降水、风、湿度、气压、云量等，适合作为 Open-Meteo 交叉核验。
- 规则：必须使用能识别应用和联系方式的真实 User-Agent；尽量用 `If-Modified-Since`，坐标最多 4 位小数；单应用超过 20 requests/s 可能被限流，没有 SLA。官方条款明确禁止故意绕过限流或冒充其他客户端。
- 许可：开放数据按 CC BY 4.0 署名；产品/底层模型的具体限制仍需查产品页。
- 失败时回到 Open-Meteo；不要通过增加并发或更换 UA 解决 403/429。

## 新闻、发布会和伤停舆情

### 官方优先

- 先抓官方联赛/协会和俱乐部文章、赛前发布会文字/视频页：保存原文 URL、标题、作者/发布主体、`published_at`、抓取时间和摘要；只把明确的“out/suspended/doubtful/fit”映射到结构化状态。
- 对一场比赛至少保留双方俱乐部官方源；新闻没有结构化 API 时，可以人工审核后写入 `evidence_grade=official`，而不是让关键词模型自动修改球员可用性。
- 发布会视频的转录属于版权内容；公共链接可以作为证据，未经授权不要批量复制全文或视频。

### Google News RSS 与 GDELT：发现层

- [Google News RSS](https://news.google.com/rss/search?q=Premier+League+injury&hl=en-US&gl=US&ceid=US:en) 可返回标题、链接、来源、发布时间和媒体摘要；本机 200。Google 的新闻 feed 条款/来源权利不等于允许再发布全文；低频缓存并只存元数据，点击原站核验。
- [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) 可按语言、关键词、时间检索文章元数据，覆盖六联赛新闻；本机曾收到 429 且正文要求每 5 秒最多一次，高流量应改用其 ngrams 或联系维护者。原始文章版权仍归媒体，不把 GDELT 命中当官方事实。
- 降级路径：聚合源失败时只保留官方 allowlist；聚合源和官方内容冲突时，状态字段保持 `reported`/人工待核，不自动写 `confirmed`。

## 推荐组合、刷新策略与降级矩阵

### 最推荐的八个来源（按落地价值排序）

1. **API-Football**：注册后先做六联赛 coverage/字段试点，最有希望统一提供伤停、首发、统计和赔率；生产应按授权套餐。
2. **football-data.org v4**：透明、成本低的赛程/赛果/积分基线；五大联赛免费层较明确，中超和深度字段先验收。
3. **Sportmonks**：API-Football 覆盖不足时的商业深度备选，尤其是 lineups、sidelined、xG/odds add-on；不要把免费计划当六联赛方案。
4. **官方联赛/CFA/俱乐部页面**：事实核验、赛程变更、新闻/发布会和最终确认 XI 的最高优先级；接受人工/动态 HTML 成本。
5. **Open-Meteo**：全球赛时天气的无 key 主源，适合低频批量缓存；商业使用先处理 licence。
6. **MET Norway**：有明确 User-Agent、节流和 CC BY 规则的天气第二源。
7. **Understat**：五大联赛 xG/近期状态研究辅助；当前网页可读但隐藏接口 404，不能做唯一主源。
8. **OpenFootball/world**：CC0 的中超历史赛果（2022–2025 文件可读）和其他联赛历史补充；非实时，不能补伤停、首发、赔率。

**德甲额外推荐**：[OpenLigaDB](https://www.openligadb.de/) 无密钥、ODbL、社区维护，作为 Bundesliga 赛程/赛果降级源非常实用；它是上述第 8 项之外的联赛专用补源。赔率专用可另评 The Odds API；新闻专用可另评 Google RSS/GDELT。

### 按赛前时间窗的最小刷新策略

| 时间窗 | 必抓字段 | 推荐频率/动作 | 通过条件 |
|---|---|---|---|
| T-7d 至 T-48h | 赛程、场地、积分/状态、天气初报、开盘赔率 | 结构化源按 6–12h；天气按 6–12h；赔率按授权额度 | 比赛 ID、UTC/local 时间、主客队映射稳定 |
| T-48h 至 T-6h | 赛程变更、伤停、发布会、状态、赔率 | 2–4h；API-Football injuries 不高于文档建议；官方新闻按发布时间增量 | 伤停有来源和发布时间，天气保留模型生成时间 |
| T-6h 至 T-75min | 伤停、天气、盘口、俱乐部赛前信息 | 30–60min；仅拉必要比赛，缓存字典 | 只纳入 `effective_at <= as_of` |
| T-75min 至 kickoff | **确认 XI**、最终伤停、盘口、天气 | 对支持的授权 endpoint 做 1–2 次；不对受限站点狂刷 | 只把官方/授权 provider 明确的 starting XI 标为 confirmed |
| kickoff 后 | 结果、事件、赛中统计（若研究需要） | 与赛前快照隔离；不可回写赛前特征 | 赛前模型冻结，防止标签泄漏 |

### 字段缺失时的明确降级

| 缺失 | 允许的处理 | 禁止的处理 |
|---|---|---|
| 赛程/结果 | 切换 football-data.org ↔ API-Football ↔ 官方页；OpenLigaDB/OpenFootball 仅按覆盖范围使用 | 用旧缓存冒充当前，或将动态 HTML 解析失败当空赛程 |
| 伤停 | 标记缺失；用官方人工核验或另一授权 provider 补；模型输出 `research_only` | 用新闻关键词/旧阵容直接标 out，或用 Elo 代替伤停 |
| 确认首发 | 等待授权/官方确认；仍保持 blocked | 把 expected XI、媒体预测、最近一场首发标 confirmed |
| xG | 五大联赛尝试 Understat/FBref/授权 API；CSL 空值并走独立 Elo/比分状态 | 把进球率或 Elo 改名成 xG |
| 赔率/盘口 | 无市场就返回空值并不计算市场概率；切换授权 provider | 通过 WAF、验证码、UA 伪装访问；把旧赔率标 current |
| 新闻/发布会 | 只用官方 allowlist 或将聚合结果标 reported | 复制全文、把聚合标题视作官方确认 |
| 天气 | Open-Meteo ↔ MET Norway；两者失败则缺失 | 用城市常年均值伪造赛时天气 |
| 积分/战意 | 用同一 `as_of` 的 standings 和明示规则派生 | 使用赛后积分、最终排名或未经来源支持的主观战意标签 |

## 上线前验收清单

对每个供应商、联赛、赛季、字段至少做一场近期已结束比赛和一场未来比赛：

- 记录 HTTP 状态、响应 schema、字段非空率、provider 原生 ID、时区和时间精度；
- 验证未来比赛是否能在 T-48h/T-6h/T-75min 看到伤停、盘口和确认 XI，而不是只测试赛后数据；
- 对六联赛分别报告覆盖率；不能用五大联赛的成功率代表 CSL；
- 对赔率记录 bookmaker、market、币种/格式、去水方式、更新时间和许可；
- 对新闻记录原始链接、来源主体、发布时间、语言和证据等级；不保存无权复制的正文；
- 对天气记录球场坐标和 forecast generation time，避免把重新查询的事后天气写进历史回测；
- 对所有拒绝（401/403/429/567/Cloudflare challenge）做有限重试和退避，达到阈值就降级并报警；不通过绕过访问控制取得“成功”；
- 原始快照和哈希分开于标准化表，保留 parser/provider 版本，便于回放和解释；
- 没有伤停和确认首发时，预测接口显式返回 `research_only`/`unavailable`，而不是隐藏门禁。

## 主要链接与许可核验入口

- [football-data.org pricing](https://www.football-data.org/pricing) / [policies](https://docs.football-data.org/general/v4/policies.html) / [terms](https://www.football-data.org/about)
- [API-Football coverage](https://www.api-football.com/coverage) / [pricing](https://www.api-football.com/pricing) / [terms](https://www.api-football.com/terms)
- [Sportmonks pricing](https://www.sportmonks.com/football-api/plans-pricing/) / [rate limits](https://docs.sportmonks.com/v3/api/rate-limit)
- [OpenLigaDB license and API](https://www.openligadb.de/) / [documentation](https://openligadb.readthedocs.io/en/latest/)
- [OpenFootball/world CC0 repository](https://github.com/openfootball/world)
- [StatsBomb Open Data terms](https://github.com/statsbomb/open-data)
- [Open-Meteo docs](https://open-meteo.com/en/docs) / [pricing and commercial license](https://open-meteo.com/en/pricing)
- [MET Norway Terms of Service](https://api.met.no/doc/TermsOfService)
- [The Odds API v4 docs](https://the-odds-api.com/liveapi/guides/v4/)
- [GDELT DOC API announcement/docs](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [Google News feed terms](https://www.google.com/intl/en_us/news_feed_terms.html)
