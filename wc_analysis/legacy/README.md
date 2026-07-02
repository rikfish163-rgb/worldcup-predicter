# Legacy

以下文件经全方位审查(2026-07-02)确认: 自2026-06-26初始提交后零改动, 且全仓库
(含predict.py、crontab、systemd unit)搜索无任何引用, 不在当前生产pipeline里。
归档而非删除, 保留git历史以备未来参考。

- `harness.py` — 早期自进化诊断脚本, 已被 `evolve_groupstage.py` 取代
- `backtest.py` / `backtest_v2.py` — 一次性回测脚本, 产出结论后未再运行
- `ensemble_model.py` / `fusion_predictor.py` / `comprehensive_predictor.py` —
  几套早期实验性的多模型融合尝试, 未接入生产; `fusion_predictor.py` 是四模型
  融合最完整的实现, 若未来想升级模型融合方式可能有参考价值
- `sporttery_server.py` — 旧的HTTP拉取模式, 已被当前的scp/rsync推送模式取代
- `render_0622_html.py` / `render_0622_pptx.py` — 2026-06-22那次分析报告的
  渲染脚本, 已被 `predict.py` 内联的 `render_html()` 取代

当前生产pipeline的唯一入口是 `wc_analysis/predict.py`。
