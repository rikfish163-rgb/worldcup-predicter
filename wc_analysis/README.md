# `wc_analysis` 兼容目录

本目录保存早期世界杯实验的少量回放代码和安全回归夹具。它不是
Matchline 的生产入口，也不参与当前实时抓取、四阶段冻结、OpenFootball raw
admission、模型锁或 Sites 发布。

## 当前主线

请从仓库根目录开始：

- `league_platform/`：来源账本、权利门、raw archive、历史验证、前瞻周期和发布门；
- `matchline_sites/`：面向研究者的 Sites 工作台和离线读模型；
- `deploy/systemd/`：当前运行单元。没有经过核验的来源权利、持久 raw archive
  或模型锁时，运行单元必须保持 `blocked`/`research_only`。

入口和运行边界见根目录 [README.md](../README.md) 以及：

- `docs/current-data-and-prediction-contract.md`
- `docs/prospective-cycle.md`
- `docs/strict-backtest.md`
- `docs/crawl4ai-public-source-pipeline.md`

## 仅限兼容回放

`predict.py`、`worldcup_0622_analysis.py`、`standings.py` 和
`sporttery_server.py` 只用于已有测试夹具和历史结果回放。它们不得被配置为
cron、systemd、VPS 发布或长期自进化任务，也不应被当作当前比赛的实时预测。

`predict.py --serve` 仅保留只读回放和手动刷新兼容能力；旧 `/api/top3` 与
`/api/retrain` 路由会明确返回 `410 deprecated`，不再调用已移出主线的生成器、
自进化或重训脚本。

```bash
cd /home/hetaisheng/soccerdata
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_wc_analysis_0622.py
```

早期 v2 集成、4090 中转、cron/VPS 发布和自进化重训脚本已经从主线移到外置
迭代归档；保留副本仅用于审计和必要恢复。不要重新启用这些入口来绕过当前
的来源权利、时间截断或发布门。
