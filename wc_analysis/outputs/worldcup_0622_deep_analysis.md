# 2026-06-22 世界杯四场深度分析

生成时间：2026-06-21 17:57:13

## 方法

- 先验：国家队 Elo + FBref 近期赛程/控球 + 射门表构造 npxG 代理，进入 Dixon-Coles 双泊松比分矩阵。
- 校准：伤病/阵容可信度、休息时间、天气、体彩总进球分布，对进球期望做二次修正。
- 盘口：体彩 had/hhad/ttg/crs 先去水，再和后验概率比较。结论区分“更可能发生”和“更有价值”。

## 本地博客解析后的使用边界

博客强调 SoccerData 的多源统一、缓存、FBref/ClubElo/Understat/Sofascore 数据分工。这里采用相同多源思路，但本次是国家队比赛：ClubElo 不适用，Elo 走 eloratings.net；FBref 当前缓存没有真 xG，npxG 是射门/射正代理；体彩 API 是实时盘口校准源。

## 四场结论

### 周日037 西班牙 vs 沙特阿拉伯 (2026-06-22 00:00 北京时间)

- 场地：Atlanta Stadium, Atlanta
- 体彩：让球-2.00 主55.0%/平19.5%/客25.6%；总进球热项 4球 20.2%
- Elo：2129 vs 1598，差值 +531
- 先验胜平负：主73.5% / 平19.1% / 客7.4%
- 校准后胜平负：主75.5% / 平17.1% / 客7.4%
- 进球期望：2.28 - 0.58
- 热门比分：2-0(14.8%), 1-0(12.5%), 3-0(11.3%), 2-1(8.7%), 1-1(8.0%)
- 结论：西班牙胜；让-2不追深，比分2-0/3-0优先；信心 中高

校准依据：
- Yamal 可首发但分钟受控：提升上半场爆点，同时压低全场大胜上限。
- 沙特核心仍以低位防守和反击为主，若早失球，防守折损更大。
- 用体彩总进球分布做总量校准：市场均值约3.82球。

风险提示：
- 体彩未开标准胜平负，胜面只能用让球/比分/总进球交叉验证。

### 周日038 比利时 vs 伊朗 (2026-06-22 03:00 北京时间)

- 场地：Los Angeles Stadium, Los Angeles
- 体彩：胜平负 主69.2%/平19.2%/客11.6%；让球-1.00 主46.1%/平23.8%/客30.1%；总进球热项 3球 23.5%
- Elo：1879 vs 1756，差值 +123
- 先验胜平负：主46.6% / 平27.4% / 客26.0%
- 校准后胜平负：主45.4% / 平27.4% / 客27.2%
- 进球期望：1.49 - 1.10
- 热门比分：1-1(13.1%), 1-0(10.5%), 2-1(9.2%), 2-0(8.4%), 0-0(8.3%)
- 结论：比利时胜面仍在，但直胜低赔和让-1都不值；比分1-0/2-1，小心1-1；信心 中

校准依据：
- Doku 因病缺阵，右路推进和一对一爆点明显下调。
- 伊朗组织性强，市场对平/小比分保护更充分。
- 用体彩总进球分布做总量校准：市场均值约2.95球。

### 周日039 乌拉圭 vs 佛得角 (2026-06-22 06:00 北京时间)

- 场地：Miami Stadium, Miami
- 体彩：胜平负 主68.1%/平21.9%/客10.1%；让球-1.00 主42.2%/平26.8%/客31.0%；总进球热项 2球 26.1%
- Elo：1870 vs 1606，差值 +264
- 先验胜平负：主67.6% / 平21.7% / 客10.8%
- 校准后胜平负：主65.6% / 平23.1% / 客11.3%
- 进球期望：1.79 - 0.60
- 热门比分：1-0(15.8%), 2-0(14.7%), 1-1(10.4%), 0-0(9.7%), 2-1(8.8%)
- 结论：乌拉圭胜最稳；让-1接近公平偏谨慎，1-0/2-0优先；信心 中高

校准依据：
- Araujo 和 De Arrascaeta 缺阵，分别削弱防线稳定和中路创造。
- 佛得角抗压和定位球路径是冷门脚本，但持续进攻创造力有限。
- 用体彩总进球分布做总量校准：市场均值约2.48球。

风险提示：
- 射门样本不足，npxG代理权重应下调。

### 周日040 新西兰 vs 埃及 (2026-06-22 09:00 北京时间)

- 场地：BC Place, Vancouver
- 体彩：胜平负 主14.8%/平22.9%/客62.4%；让球+1.00 主36.0%/平26.8%/客37.2%；总进球热项 2球 24.9%
- Elo：1578 vs 1711，差值 -133
- 先验胜平负：主31.8% / 平28.1% / 客40.2%
- 校准后胜平负：主31.2% / 平27.9% / 客40.9%
- 进球期望：1.19 - 1.39
- 热门比分：1-1(13.3%), 0-1(9.8%), 1-2(8.7%), 0-0(8.4%), 1-0(8.3%)
- 结论：埃及不败/埃及胜；让+1方向新西兰有保护价值；信心 中

校准依据：
- Garbett 退出名单削弱新西兰中场创造，但 Wood/Just 的高球和定位球路径仍需要保留。
- 埃及进攻核心质量更高，胜面强于新西兰，但客让一球市场已明显压价。
- 用体彩总进球分布做总量校准：市场均值约2.64球。

## 数据可靠性检查

- 体彩：本轮 live API 可抓，四场 matchId 为 2040247-2040250；盘口随时间会动，赛前需重跑。
- FBref：使用本地缓存 HTML；对强弱悬殊队伍，预选赛样本质量差异大，因此 npxG 代理仅用于方向校正。
- 天气：open-meteo 逐小时预报，无 API key；室外场地才强影响，预报仍有临场误差。
- 伤病：当前为公开新闻和阵容深度校准，未等同官方首发；临场名单会显著影响让球盘。

## 来源

- [FIFA match centre](https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/match-center)：赛程、场地和开赛时间主核验。
- [Guardian: Yamal availability](https://www.theguardian.com/football/2026/jun/20/lamine-yamal-genius-salvador-dali-michelangelo-spain-luis-de-la-fuente-world-cup)：西班牙 Yamal 可首发但分钟受控。
- [Al Jazeera: Yamal minutes](https://www.aljazeera.com/sports/2026/6/19/spains-yamal-says-very-early-unnecessary-to-play-full-world-cup-match)：Yamal 伤后恢复和不必踢满 90 分钟。
- [AP/TheScore: Doku out](https://www.thescore.com/belpro/news/3556178/belgiums-doku-will-miss-world-cup-match-with-iran-due-to-illness)：比利时 Doku 因病缺阵。
- [RotoWire lineups](https://www.rotowire.com/soccer/lineups.php?league=WOC)：阵容/伤停交叉核验。
- [RotoWire: Uruguay vs Cape Verde](https://www.rotowire.com/soccer/article/uruguay-vs-cape-verde-preview-predicted-lineups-team-news-tactical-analysis-2026-world-cup-group-h-118935)：Araujo、De Arrascaeta 缺阵和佛得角伤停。
- [Washington Post/AP: Garbett out](https://www.washingtonpost.com/sports/soccer/2026/06/15/world-cup-new-zealand-injury-garbett-iran/827afa10-6919-11f1-830e-133d20cadd28_story.html)：新西兰 Garbett 退出名单。
- [The National: Egypt camp](https://www.thenationalnews.com/sport/world-cup-2026/2026/06/21/egypt-boss-denies-mohamed-salah-rift-ahead-of-vital-world-cup-clash-against-new-zealand/)：埃及 Salah 相关传闻和防守高球调整。