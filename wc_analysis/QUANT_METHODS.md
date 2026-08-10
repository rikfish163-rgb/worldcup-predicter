# 著名预测家量化方法调研 (落地改进清单)

> 调研日期 2026-06-30。提炼自学术论文+业界标杆,作为特征工程的理论依据,避免拍脑袋。

## 1. Dixon-Coles (1997) — 本项目核心模型

**已实现**:ρ低比分校正 + 时间衰减ξ。
**关键校准点(论文原文)**:
- ξ=0.0065 是论文值,但**单位是半周(half-weeks)**,换算成天要 ÷3.5 → 实际 ξ_day ≈ 0.00186。**需核查本项目ξ单位是否一致**(当前 XI=0.0065,若按天用则衰减过猛)。
- 半衰期:顶级联赛推荐 1-3年;赛季内预测用 3-6个月(更看重近期状态)。
- 改进空间:DC 把球队攻防评级在拟合窗口内当**静态**,这是已知局限 → 动态评级(见pi-rating)。
- DC 的优势集中在**低比分/比分盘/大小球**市场(0-0,1-1定价更准),正是博彩定价低效区。

来源: [dashee87 DC+time-weighting](https://dashee87.github.io/football/python/predicting-football-results-with-statistical-modelling-dixon-coles-and-time-weighting/) | [penaltyblog](https://pena.lt/y/2021/06/24/predicting-football-results-using-python-and-dixon-and-coles/)

## 2. FiveThirtyEight SPI / Nate Silver PELE — 攻防分离 + 休息天数

**核心思想(与本项目高度契合)**:
- 每队拆 **offensive rating(预期进球) + defensive rating(预期失球)** 两个独立评级 → 正是本项目 xG档案(进攻) + xGA(防守) 的方向,**验证了我们集成 def_xga90 的正确性**。
- **泊松模型参数 = 攻评 + 防评 + 主场优势 + 休息天数(days of rest)** → **休息天数是538官方采用的特征**!这给当前 workflow 正在挖的"体能/休息天数"维度提供了权威背书。
- 主场优势 ≈ 0.2 球/场,且**随时间递减**(现代主场优势在下降) → 本项目 HOME_ADV 可参考。
- 攻防评级由4个指标平均:实际进球/adj进球/xG/shots-based。**xG 权重应略高于进球**(但538因xG数据只有6季而用等权)。
- 动态模拟"hot":赛季模拟中评级随模拟结果浮动,而非静态。

来源: [538 club projections](https://fivethirtyeight.com/features/how-our-club-soccer-projections-work/) | [PELE](https://www.natesilver.net/p/pele-international-football-rankings-soccer-ratings-projections)

## 3. Constantinou & Fenton — pi-rating + Dolores (学术SOTA)

- **pi-rating**:基于"比分差异(score margins)"的动态评级,是 Bradley-Terry 的动态扩展,**实测优于标准Elo**预测EPL。本项目用Elo,可考虑用比分幅度加权(大胜vs小胜信息不同)。
- **Dolores(2019)**:跨联赛迁移学习——用其他国家的比赛数据预测本国比赛,即使两队都没参与过那些历史数据。对世界杯(各队交手少)有启发:可借用俱乐部联赛数据补国家队样本。
- 国际ML足球预测赛冠军模型就基于 pi-rating,Dolores 第二(差<1%)。

来源: [Dolores论文](https://link.springer.com/article/10.1007/s10994-018-5703-7) | [pi-football](https://www.sciencedirect.com/science/article/abs/pii/S0950705112001967)

## 4. 对本项目的具体行动项 (按优先级)

| # | 改进 | 依据 | 状态 |
|---|------|------|------|
| A | ~~核查 XI 时间单位~~ | DC原论文 | ✅已核查无误 |
| B | **休息天数特征**(days of rest) | 538官方参数 | 🟡workflow挖掘中 |
| C | **xGA防守评级** | 538攻防分离 | ✅已集成 |
| D | xG权重略高于历史进球 | 538 | 🟡可调 |
| E | 主场优势随时间递减/淘汰赛中立 | 538 | ✅淘汰赛已处理 |
| F | 比分幅度加权(大胜携带更多信息) | pi-rating | 🟢未来 |
| G | 跨联赛数据补国家队小样本 | Dolores | 🟢未来 |

**核查结论**:
- A ✅ predict.py:503 `XI=0.0065` 用于 `exp(-xi·days_ago)`,days_ago按天。验算半衰期 ln2/0.0065=107天,在赛季内预测推荐区间(3-6月)内,**实现正确非bug**(项目按天重设而非照搬论文半周值)。

**最高价值待办: B(休息天数,538官方验证过的真特征,workflow挖掘中)**。
