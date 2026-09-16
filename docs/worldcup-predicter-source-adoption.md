# worldcup-predicter 来源复用边界

`rikfish163-rgb/worldcup-predicter` 已作为 Matchline 的来源适配参考，而不是把旧项目整套运行链原样搬进生产。

## 已复用的安全部分

- `wc_analysis/scrape_sporttery.py` 的体彩多池数据形状、去水概率和比分/总进球/半全场字段，落在 [sports_lottery.py](/home/hetaisheng/soccerdata/league_platform/live_sources/sports_lottery.py) 的固定 HTTPS 白名单适配器中。
- `wc_analysis/match_results.py` 的赛果规范化思路用于历史结果核验；当前正式赛果以通过 raw admission 的 OpenFootball 为准，OpenLigaDB 只作德甲 display/post-match 交叉。
- 项目 README 中列出的 ESPN、Understat、RSS/Google News、Open-Meteo、SofaScore、公开赔率和 football-data/OpenFootball 历史源，已按“当前可用、对照、展示或历史训练”分层登记；当前权利未核验的源默认隔离，不代表可自动抓取或再分发。
- README 同时列出的 FBref、WhoScored、ClubElo、SoFIFA 也完成了边界审计：FBref 当前公开页面在本机返回 403，ClubElo 当前握手超时，SoFIFA robots 明确限制 AI 训练且其 API 路径禁止，WhoScored 只声明部分路径可抓取。它们没有被伪装成当前可用源；需要重新验证 robots、条款和稳定性后才能显式加入登记。

完整登记见 [source_registry.py](/home/hetaisheng/soccerdata/league_platform/source_registry.py)。每个来源的轮询频率、主机、权限边界和是否可以进入模型都在同一处声明；Sites 可以据此展示来源健康，而不把“抓到网页”误认为“模型可用”。

## 明确不执行的旧逻辑

- 旧 `relay_sporttery.sh` 的 4090 中转、反 WAF/换 UA/绕访问限制逻辑已移到外置迭代归档，当前主线不接入。
- 旧 `self_evolving_loop.py` 的每日临时重训已移到外置迭代归档；模型参数只按锁定版本和周度重训规则更新。
- `wc_analysis/xg_features.py` 中的硬编码默认 xG 不进入概率模型。

如果官方公开端点返回 403、验证码、robots 禁止或重定向到未白名单主机，系统记录 `unavailable`/`blocked`，不会再找“更隐蔽的爬法”。Crawl4AI 只读取显式配置的公开 URL；未完成来源解析、实体匹配、`effective_at`/`observed_at` 和冲突审计前，页面证据保持 `display_only`。

## 轮询与模型边界

轮询分为普通周期和临近开赛周期。历史数据只能用于训练/回测；每个冻结阶段只读取该阶段截止前已经观察到的快照。官方首发、结构化伤停和可靠来源的事实可以在满足时间与冲突门槛后进入模型，情绪/舆情默认只展示。体彩赔率用于购买场景基线，OddStorm 等独立市场只用于校准和对照。
