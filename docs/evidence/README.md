# 当前证据入口

仓库只保留运行链需要的证据和当前门禁摘要。逐轮清理收据、过渡锁、候选实现与可再生
缓存已移到外置归档；本目录保留主线清理汇总及其连续增量：
[cleanup-v293-mainline-clean-2026-08-26.json](cleanup-v293-mainline-clean-2026-08-26.json)、
[cleanup-v294-mainline-prune-2026-08-26.json](cleanup-v294-mainline-prune-2026-08-26.json)、
[cleanup-v295-mainline-generated-2026-08-26.json](cleanup-v295-mainline-generated-2026-08-26.json)、
[cleanup-v296-post-test-logs-2026-08-26.json](cleanup-v296-post-test-logs-2026-08-26.json)、
[cleanup-v297-final-audit-tmp-2026-08-26.json](cleanup-v297-final-audit-tmp-2026-08-26.json) 和
[cleanup-v298-full-test-logs-2026-08-26.json](cleanup-v298-full-test-logs-2026-08-26.json)。
上述收据按时间顺序记录生成物和回归日志的可恢复移动。

历史候选锁迁移只读核验和隔离演练已从主线证据目录移出，原文件按原字节保存在外置归档：
`/media/hetaisheng/044A81D94A81C83E/soccerdata-iteration-archive-2026-08-26/docs-mainline-history-2026-08-26/retired-evidence-v294/lock-migration-v287-readiness-2026-08-26.json`。
它不代表活动锁已迁移或模型已达到发布门。后续候选锁也只保存在外置隔离归档中，未进入本目录或当前运行链；任何模型输入改动后都必须重新生成并现场校验。

## 当前保留

- `prospective-model-lock-current.json`：唯一活动 runtime-only 锁。锁仍可能处于
  `pending_prospective_window`，名称不代表已达到发布门。
- `prospective-cycle-latest.json`、`prospective-evaluation-current.json`：兼容 CLI 夹具；生产真值写在 runtime
  根目录，仓库夹具可能滞后，不能覆盖 runtime 真值。
- `maturity-gate-current.json`、`publication-audit-current.json`、`model-selection-audit-current.json`：仓库内的
  兼容门禁/审计摘要；真正的运行时版本必须从 `/dev/shm/matchline-live-runtime/` 读取并现场重验。
- 旧 v259/v260/v16 锁和报告已从生产证据目录移出；仅在 `tests/fixtures/evidence/` 保留最小兼容夹具。

`wc_analysis/legacy/`、0622 静态报告、旧 cron/VPS 发布入口和未晋级候选实现不再位于主线；`wc_analysis/predict.py`
等少数兼容入口仍保留，是为了让历史读取和安全回归明确失败或保持研究模式，不是生产发布入口。

## 运行真值

- runtime：`/dev/shm/matchline-live-runtime`
- OpenFootball durable raw archive：`/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime/openfootball-raw`
- Sites 快照：`matchline_sites/public/offline_snapshot.json`
- Sites 构建回执：`matchline_sites/.runtime/sites-build-resource.json`

仓库 dated evidence 仅作不可变审计输入或测试夹具。锁、raw archive、周期、评估、权利或 publication 绑定
失败时必须保持 `blocked`/`research_only`，不能用旧文件或格式正确的自报摘要补齐。

本地预览重启时可能重新生成被 `.gitignore` 忽略的 `matchline_sites/.wrangler/`；它是服务缓存，不是主线证据，
不会被发布链读取。

## 可恢复归档

完整旧收据和历史运行残余仍在：

`/media/hetaisheng/044A81D94A81C83E/soccerdata-iteration-archive-2026-08-26`

此前各轮运行残余、Node 编译缓存、Sites Wrangler 状态、日期化实施计划和淘汰文档都在
`cache/` 与 `docs-mainline-history-2026-08-26/` 下按原收据归档；本轮汇总收据记录了
文件清单、SHA-256、回归结果和未执行的外部写操作。归档采用可恢复移动，没有执行 Git reset、删除历史数据、D1 写入或公网部署。
