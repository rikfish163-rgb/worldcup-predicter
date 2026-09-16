# Crawl4AI 公开来源执行链

Crawl4AI 是浏览器/页面抓取执行层，不是信息源本身。每个页面必须声明独立的事实来源身份、URL
allowlist、解析契约、robots 结果、最终 URL、抓取时间、内容哈希和模型准入；抓取成功不等于事实源
可信，也不等于可以训练或再分发。

## 运行边界

1. 先查来源权利、robots、TLS、重定向、大小和限速，再决定是否联网。
2. 单主机串行限速，跨主机只使用有界并发；超时、403、TLS、解析和 robots 失败只隔离该来源。
3. 不绕过登录、验证码、WAF、robots 或访问限制，不使用代理/UA 轮换伪造成功。
4. 页面内容写入 append-only raw archive；相同内容按 SHA-256 去重，冲突保留原始记录。
5. 详情页只能由来源自己的索引解析器发出，不能扫描任意 DOM 链接或扩大抓取范围。
6. 没有明确商业权利的来源保持 `rights_blocked`/`model_eligible=false`，不因配置为 enabled 而放行。

## 当前来源分层

| 层级 | 例子 | 当前用途 |
|---|---|---|
| 已有明确权利 | OpenFootball CC0、Wikidata CC0 | 仅在 raw admission 与实体/时间校验通过后进入 current；OpenFootball 才可进入正式历史/前瞻链 |
| 有限制的公开源 | OpenLigaDB ODbL、MET Norway、OSM | 按各自 attribution/share-alike/use-case 规则隔离；OpenLigaDB 仅 display/post-match 交叉 |
| 权利未证实或访问受限 | ESPN、SofaScore、WhoScored、FBref、ClubElo、OddStorm、未知 Crawl4AI 页面 | 保留阻断诊断或 quarantine，不联网、不进入模型、不进入公开再分发包 |

## 观测合同

每条记录同时保存 `effective_at` 和 `observed_at`；发布时间缺失时只能使用首次观察时间并注明原因。
来源冲突按来源等级、发布时间、首次观察时间、重复确认和实体匹配置信度处理；无法解决的冲突不会
直接进入最终模型。

生产 unit 使用 `/home/hetaisheng/soccerdata/config/crawl4ai_operator_public.json`，但 operator
配置不能提升未知权利。当前周期的真实状态以 runtime `current.json` 与 `source_runs` 为准，仓库中的
历史探针仅供审计，不作为 fresh 证明。

## 回归入口

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  tests/test_crawl4ai_source.py tests/test_crawl4ai_runtime.py \
  tests/test_v260_blocked_source_fetches.py
```

任何来源门失败都必须在网络调用前返回结构化阻断；不能通过把错误改成 `fresh`、把执行层计数当作
事实源计数或把页面行标成 OpenFootball 来“修绿”。
