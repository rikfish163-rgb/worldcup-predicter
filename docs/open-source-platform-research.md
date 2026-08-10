# 开源足球预测平台技术调研

调研日期：2026-08-10。目标是为英超、西甲、德甲、意甲、法甲和中超建立可复现、可校准、
可解释的比赛预测平台。结论来自项目原仓库、官方文档和论文；没有把第三方“预测结果 API”
当作独立模型证据。

## 结论

没有一个项目能同时满足六联赛、实时、开源、稳定维护和生产级数据许可。推荐采用组合式架构：

```text
Provider adapters
  -> canonical fixture / team / market schema
  -> league-specific Elo + Dixon-Coles baseline
  -> optional xG / ML features with coverage gates
  -> chronological out-of-fold calibration
  -> walk-forward evaluation
  -> read-only API and dashboard
```

## 优先学习和采用

| 项目或来源 | 作用 | 许可证/性质 | 决策 |
|---|---|---|---|
| [penaltyblog](https://github.com/martineastwood/penaltyblog) | Poisson、Dixon-Coles、双变量 Poisson、Bayesian、赔率去水 | MIT | 作为模型实现和概率网格基准，先对照验证再决定是否新增依赖 |
| [soccerdata](https://github.com/probberechts/soccerdata) | FBref、Understat、ClubElo、ESPN、Sofascore、Football-Data 适配 | Apache-2.0 | 保留抓取层，在其上增加统一契约、来源时间和质量门禁 |
| [football-data.org](https://www.football-data.org/coverage) | 当前赛程、结果、积分 | 免费层 + 商业层 | 五大联赛的首选 API 候选；中超覆盖和套餐需实测 |
| [openfootball/world](https://github.com/openfootball/world) | 公开历史赛程与结果，包含中国超级联赛目录 | CC0-1.0 | 已接入中超 2022–2024 历史结果和可复现测试，不自动等同实时生产源 |
| [StatsBomb Open Data](https://github.com/statsbomb/open-data) | 高质量事件和部分 360 数据 | 定制开放数据许可 | 只用于研究和特征验证，不作为六联赛实时主源 |
| [Kloppy](https://github.com/PySport/kloppy) | 统一事件与追踪数据 | BSD-3-Clause | 有授权事件数据后作为可选标准化层 |
| [ClubElo](https://clubelo.com/Data) | 欧洲俱乐部历史 Elo | 数据许可需单独确认 | 欧洲强度先验；不能承担中超强度 |
| [football-data.co.uk](https://www.football-data.co.uk/) | 五大联赛历史比分和赔率 CSV | 免费访问，分发许可需确认 | 当前历史市场基线；不在未确认许可前重新分发原始文件 |
| ESPN scoreboard | 六联赛当前赛程、结果和原生球队/比赛 ID | 公共读取端点，使用条款需持续复核 | 已作为当前 fixture 主源，原始响应哈希和抓取时点必须保留 |
| Understat | 五大联赛比赛 xG 与近期状态 | 公共站点，抓取条款需持续复核 | 已作为欧洲近期 xG 辅助源，不覆盖中超也不替代赛程主源 |

明确不作为生产核心：

- 已归档的 `worldfootballR`；
- 不再积极开发的 `socceraction` 主包，只保留算法参考；
- 供应商提供的黑盒比赛预测；
- 没有采集时间、原始快照和来源哈希的临时网页数据；
- 直接将五大联赛参数复制给中超。

## 数据覆盖策略

| 数据用途 | 五大联赛 | 中超 | 缺失处理 |
|---|---|---|---|
| 历史比分 | MatchHistory / OpenFootball | OpenFootball 2022–2024 已接入 | 无来源时标记不可用 |
| 当前赛程与结果 | football-data.org / Sofascore | football-data.org 或授权 API 待验证 | 不用旧缓存显示为实时 |
| 历史赔率 | football-data.co.uk | 授权数据待定 | 无赔率不计算市场概率 |
| xG / 事件 | Understat、FBref、授权事件源 | 当前没有稳定公开源 | 保持空值，不用 Elo 冒充 xG |
| 球队强度 | ClubElo + 联赛内动态 Elo | 自建联赛内 Elo | 独立初始化和主场参数 |

## 模型方法

第一阶段以可解释基线为准：

1. 每个联赛独立的动态 Elo；
2. 时间衰减的进攻/防守强度；
3. Dixon-Coles 低比分相关修正；
4. 从比分概率网格派生 1X2、大小球和比分分布；
5. 有覆盖门禁的 xG、休息天数、主客场状态和阵容特征；
6. 市场概率作为独立 baseline 或校准输入，而非标签替代品。

Context7 检索的 [scikit-learn 概率校准文档](https://scikit-learn.org/stable/modules/calibration.html)
指出：校准数据必须与模型拟合数据隔离，小样本不适合 isotonic；多分类可优先评估温度缩放或
sigmoid。`TimeSeriesSplit` 应使用扩展窗口和必要的 `gap`，确保测试比赛晚于训练比赛。

## 评估门禁

每个联赛分别报告：

- 多分类 Brier score；
- Log loss；
- Ranked Probability Score；
- calibration curve、ECE 和 sharpness；
- 样本量、覆盖率、训练截止时间和模型版本；
- 与历史结果频率、动态 Elo、去水市场概率的对比。

ROI、CLV、EV 和 Kelly 只能作为独立研究指标，不能替代概率质量，也不在默认用户界面中形成
购买建议。
