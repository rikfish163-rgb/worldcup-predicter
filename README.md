# Matchline

面向足球研究者和从业者的比赛研究工作台。主线将历史训练/回测、赛前冻结预测、赛中/赛后事实和
来源审计分开，展示概率与证据，但在样本、权利、时间或校准门禁未通过时不会给出保证性投注结论。

## 当前边界

- 赛程与赛果优先使用 OpenFootball CC0；每条记录保留真实来源、provider ID、双时钟、raw hash 和
  确定性身份。中足联等权利未完成核验的来源不进入正式再分发或模型输入。
- OpenLigaDB 仅作德甲 ODbL display/post-match 交叉；ESPN、SofaScore、WhoScored、FBref、ClubElo、
  OddStorm 和未知权利 Crawl4AI 页面默认 fail-closed。
- 服务器事实桥运行在独立 VPS `ubuntu@170.106.198.250`：当前快照包含 8 个 OpenFootball 2026-27
  联赛源与 OpenLigaDB 德甲/德乙/德丙；Wikidata、MET、ESPN、SofaScore 只保留真实的阻断、未配置或
  未准入诊断，不会把它们的空响应写成可用数据。
- Crawl4AI 是共享抓取执行层，不是信息源。它遵守 robots、TLS、重定向、限速和响应大小边界，不能
  绕过登录、验证码、WAF 或访问限制。
- 预测按 `t_minus_24h`、`t_minus_6h`、`t_minus_90m`、确认首发四阶段冻结。任何阶段只读取 cutoff
  前观察到的事实；缺失不补零，冲突不强行合并。
- 当前产品状态仍是研究模式：正式样本、独立玩法评分、市场对照、成熟度和发布审计未全部通过。

## 本地运行

使用项目虚拟环境。只读检查和单元测试不会写远端 D1：

```bash
cd /home/hetaisheng/soccerdata
source .venv/bin/activate

# 本地 Matchline Sites 工作台
cd matchline_sites
npm run dev -- --host 127.0.0.1 --port 3000

# Python 定向回归
cd /home/hetaisheng/soccerdata
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  tests/test_openfootball_raw_archive.py \
  tests/test_prospective_cycle.py \
  tests/test_prospective_evaluation.py \
  tests/test_crawl4ai_source.py \
  tests/test_crawl4ai_runtime.py
```

生产式前瞻链由用户 systemd 运行，不建议直接用默认路径启动：

- `matchline-prospective-cycle-runtime-only.timer`：每 15 分钟同步、冻结、评估；
- `matchline-runtime-readmodel.timer`：从同一 runtime 证据构建离线 Sites 快照；
- `matchline-live-overlay.timer`：独立赛中只读叠加，权利或存储门失败时写明确空状态。

当前 runtime 根目录为 `/dev/shm/matchline-live-runtime`，OpenFootball durable raw archive 为
`/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw`。模型锁为
`docs/evidence/prospective-model-lock-current.json`。存储门、锁完整性和 raw archive 门失败时，
服务会停止写入并保留上一份可验证资产。

## 代码入口

| 区域 | 入口 |
|---|---|
| 当前抓取与来源账本 | `league_platform/sync_live.py`, `league_platform/live_sources/` |
| VPS facts-only 采集与 Sites relay | `deploy/remote-facts/matchline_facts_collector.py`, `deploy/remote-facts/publish_remote_facts.py` |
| OpenFootball raw admission | `league_platform/openfootball_raw_archive.py`, `league_platform/sources/openfootball_verified.py` |
| 前瞻冻结与评估 | `league_platform/capture_prospective.py`, `league_platform/prospective_cycle.py`, `league_platform/prospective_evaluation.py` |
| 严格报告与模型锁 | `league_platform/strict_report.py`, `league_platform/prospective_lock.py` |
| Sites 离线读模型 | `league_platform/build_offline_bundle.py`, `matchline_sites/` |
| 权利门 | `league_platform/source_rights.py`, `league_platform/fixture_serving.py` |

## 发布规则

发布器必须在任何远端写入前通过：来源权利和 provenance、raw archive replay、模型锁与周期绑定、
严格报告、独立评估、Sites/D1 一致性、maturity 和 publication receipt。当前所有正式发布入口均应
保持 dry-run 或 fail-closed；未配置经过核验的 D1 凭据时不做远端写入。

## 文档

- [当前数据与预测契约](docs/current-data-and-prediction-contract.md)
- [前瞻周期主线](docs/prospective-cycle.md)
- [严格赛前回测主线](docs/strict-backtest.md)
- [Crawl4AI 公开来源执行链](docs/crawl4ai-public-source-pipeline.md)
- [来源研究主线](docs/source-research-2026-08.md)
- [当前商业状态](docs/commercial-readiness.md)
- [证据入口](docs/evidence/README.md)

## 免责声明

Matchline 输出的是带时间和证据边界的研究概率，不是保证性预测或投注指令。历史表现、模型概率和
市场差异都不能保证未来收益；在生产门禁未通过前，不应将其用于付费会员承诺或资金决策。

项目代码按仓库许可证发布；外部数据的许可、归属和再分发边界以各来源合同及
`league_platform/source_rights.py` 的当前策略为准。
