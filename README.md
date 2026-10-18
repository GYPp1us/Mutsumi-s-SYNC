# Mutsumi's SYNC V4

V4 是基于 NapCat 的个人社交 Agent 重构主线，目标运行时为 Rust。模型保留自主交流和工具编排能力，宿主负责来源身份、动作审查、持久化与真实执行结果。

当前状态：`v4` 已发布并成为 GitHub 默认分支，V3 已封存；Rust 新运行时尚未实现。

## 设计入口

- [当前 V4 架构契约](docs/current-design.md)
- [本轮已确认的实现决定](docs/refactor-decisions-2026-10-18.md)
- [V3 归档与 V4 交接](docs/v4-handoff-2026-10-18.md)
- [项目章程](init.md)
- [Agent 协作规则](AGENTS.md)

## V4 核心决定

- 分阶段迁移 Rust，借鉴 pi 的小循环与拦截边界。
- Jev 做一次开局分类；简单交流选择用户维护的预制回复，复杂或不确定请求交给生成模型。
- 每轮 assistant `content` 都可提出消息，包括带 `tool_calls` 的回合；实际发送与工具动作分别经过独立 dry-run。
- 一个 content 使用外层 JSON 的 `messages` 数组拆分消息，内层 `segments` 数组进行行内编排；每条消息必须明确目标。
- md2svg + resvg 运行于独立常驻 worker；所有成功发送和工具效果以真实回执为准。

## V3 存档

完整 Python 代码、原型、实验入口、测试与历史设计位于 [archive/v3/](archive/v3/README.md)。它们保留为行为基准和恢复资料。

V3 冻结分支为 `archive/v3`，快照标签为 `archive/v3-final-2026-10-18`。独立实验的标签与本地恢复包见 [归档索引](archive/README.md)。

HZ 的 V3 服务已经归档并停止、禁用自启。新版本部署须在实现与验收完成后另行执行。
