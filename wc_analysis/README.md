# ⚽ 足球比赛预测系统 v2 (wc_analysis)

> Dixon-Coles 模型 × 平局逻辑回归 × 风格相克 × 体彩竞彩盘口校准 — 预测未来三天在售比赛

---

## 快速使用

```bash
cd ~/soccerdata

# 一次性运行: 抓数据 → 建模 → 生成网页
.venv/bin/python wc_analysis/predict.py

# 启动本地服务(支持页面上点"重新抓取"实时刷新)
.venv/bin/python wc_analysis/predict.py --serve
# → 浏览器打开 http://localhost:8026
```

---

## 🆕 v2 新特性

### 1. **平局逻辑回归模型(8特征)**
   - Elo差、平均Elo、平方差、球队平局率、低比分倾向、失球数、历史交锋平局率
   - 修正 Dixon-Coles 原生平局概率偏差

### 2. **风格相克系统**
   - 读取 `data/style_matchup.json` 分析进攻/防守风格相性
   - 自动调整λ强度(±5%)

### 3. **磨合度因子**
   - 读取 `data/cohesion.json` 识别新组建/混编阵容
   - 降低磨合度低球队的进攻力(最多-10%)

### 4. **角球能力档案**
   - 读取 `data/corners.json` 记录场均角球数、转化率
   - 影响低比分场次进球期望

### 5. **xG档案**
   - 读取 `data/xg_profiles.json` 补充 FBref xG/npxG 数据
   - 修正 Elo 对进攻力的估计偏差

---

## 系统架构

```
体彩 sporttery API ─┐
eloratings.net     ─┤
天气 open-meteo    ─┤                 ┌─ Dixon-Coles 先验
伤病 injuries.json ─┼→ predict.py ─→ ├─ 平局逻辑回归(8特征) ─→ 对数池校准 ─→ 后验概率
风格 style_*.json  ─┤                 ├─ 风格相克调整              ↑              ↓
磨合度 cohesion.json─┤                ├─ 磨合度/xG/角球       市场去水概率     Edge(价值点)
xG/角球 *.json      ─┘                └─ 天气/伤病                              ↓
                                                                        site/index.html
```

---

## 数据文件配置

v2 新增多个可选数据文件(在 `data/` 目录下):

| 文件 | 用途 | 格式 |
|------|------|------|
| `injuries.json` | 伤病调整 | `{"队名": {"lambda_factor": 0.95, "reason": "..."}}`|
| `style_matchup.json` | 风格相克 | `{"队名": {"attack": "possession/counter/direct", "defense": "high/medium/low"}}`|
| `cohesion.json` | 磨合度 | `{"队名": {"factor": 0.92, "reason": "新帅/混编"}}`|
| `corners.json` | 角球能力 | `{"队名": {"corners_per_game": 5.2, "corners_to_goals": 0.12}}`|
| `xg_profiles.json` | xG档案 | `{"队名": {"xg_per90": 1.8, "npxg_per90": 1.5}}`|
| `draw_model.json` | 平局模型 | 自动生成(训练脚本输出)|

所有文件均可选,缺失时使用默认值。

---

## 每日自动运行

```bash
# 安装 crontab (每天 8:00/14:00/20:00 自动刷新)
crontab -e
# 加入:
0 8,14,20 * * * /home/hetaisheng/soccerdata/wc_analysis/daily_update.sh >> /home/hetaisheng/soccerdata/wc_analysis/data/cron.log 2>&1
```

---

## 手动录入伤病

编辑 `data/injuries.json`:

```json
{
  "荷兰": {
    "lambda_factor": 0.95,
    "reason": "德容膝伤存疑; Timber脑震荡缺阵"
  },
  "科特迪瓦": {
    "lambda_factor": 0.97,
    "reason": "Wahi状态存疑"
  }
}
```

- `lambda_factor < 1.0` = 进攻力打折 (0.95 = 降5%)
- 删掉队名条目 = 取消调整
- 下次运行 predict.py 自动生效(热加载)

---

## 回测验证

```bash
.venv/bin/python wc_analysis/backtest.py
```

534场国际比赛验证结果:
| 模型 | Brier↓ | LogLoss↓ |
|------|--------|----------|
| 纯Elo基线 | 0.4476 | 0.7853 |
| **Dixon-Coles v1** | **0.4467** | **0.7799** |
| **v2 (DC + 平局LR)** | **0.4451** | **0.7772** |

v2 比纯Elo更准(Brier -0.0025, LL -0.0081),平局预测准确率提升明显。

---

## 文件清单

```
wc_analysis/
├── predict.py          # 核心引擎(抓数据+建模+校准+生成网页)
├── backtest.py         # 回测验证
├── form_factor.py      # 近期状态特征(Elo趋势/波动/streak)
├── test_form_factor.py # 状态特征单元测试
├── test_corners.py     # 角球能力单元测试
├── daily_update.sh     # crontab 用自动刷新脚本
├── deploy.sh           # VPS 部署脚本(已废弃,用 deploy_v2.sh)
├── site/index.html     # 生成的预测网页(暗色主题, 带刷新按钮)
└── data/
    ├── predictions.json     # 最新预测结果
    ├── injuries.json        # 伤病录入(手动维护)
    ├── cohesion.json        # 磨合度(手动维护)
    ├── style_matchup.json   # 风格相克(手动维护)
    ├── corners.json         # 角球能力(手动维护)
    ├── xg_profiles.json     # xG档案(手动维护)
    ├── draw_model.json      # 平局逻辑回归模型(训练生成)
    ├── weather.json         # 天气(自动抓)
    ├── backtest.json        # 回测结果
    └── elo_cache/*.tsv      # Elo历史缓存(24h刷新)
```

---

## 已知局限

1. **无真 xG/npxG** — FBref 对 2026 世界杯未上 Opta 数据,用 Elo+进球历史替代
2. **伤病需手动** — 无免费实时伤停 API,需人工录入 injuries.json
3. **风格/磨合度/角球需手动** — 这些战术层数据依赖人工收集
4. **回测改善幅度中等** — 国家队数据稀疏,DC+平局LR 主要修正平局概率(纯 Elo 的痛点)
5. **联赛推广需补映射** — TEAM_DB 目前只覆盖世界杯队伍,联赛需扩充
