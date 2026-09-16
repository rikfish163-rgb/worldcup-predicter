# Matchline 网页端 UI 方向

日期：2026-08-11

本轮使用并安装了 GitHub 上的 [`funboy322/avoid-ai-design`](https://github.com/funboy322/avoid-ai-design) skill，安装位置为 `/home/hetaisheng/.agents/skills/avoid-ai-design`。它是 MIT 授权、无运行时依赖的 Agentskills 格式，适用于本项目的纯 HTML/CSS/JS 页面。

## 审计结论

- 原页面使用单一 system sans、深色仪表盘默认皮肤，容易读成生成式 SaaS 模板。
- 顶部状态、卡片和面板大量使用圆角、半透明模糊和胶囊式控件，层级被装饰稀释。
- 主要问题不是功能缺失，而是没有一个足够明确的视觉观点；数据密度、留痕和门禁信息应该成为界面本身的设计语言。

## 采用的方向

选择 **Swiss / international operational dashboard**：

1. 暖纸色背景、墨色正文和单一朱砂色信号色。
2. 中性无衬线与等宽数字的严格层级，优先可读性和扫描速度。
3. 硬边框、模块化网格、少量阴影，避免玻璃拟态和装饰性渐变。
4. 用规则线、状态点、数据条表达状态，不用图标卡片堆砌信息。

现有数据读取、筛选、主题切换、模型指标、赛程、来源状态、滚动验证、方法与门禁逻辑均保留。

## 视觉参考

`docs/design/matchline-ui-direction.png` 是使用 imagegen 生成的内部视觉参考图，仅用于校准层级和密度，不作为真实数据页面的替代品。
