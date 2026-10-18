# 设计与实验归档

这里存放已经结束、被撤回或只用于观察的设计材料和生成物。归档内容可以帮助追溯决定，但不覆盖运行时代码、测试和 [`docs/current-design.md`](../current-design.md)。

## 规则

- 当前实现以代码、测试和 `docs/current-design.md` 为准；归档文件中的流程、工具清单和时间参数都可能过时。
- 新实验先写场景、评分规则和结果清单，再把原始响应、数据库或截图放到 `data/archive/` 的日期目录；不要把大文件直接放进 `docs/`。
- 归档数据不包含密钥和生产聊天原文。需要复现时，使用报告中记录的模型、提示版本、场景版本和哈希。
- 删除归档内容前先检查 [`experiment-data-manifest-2026-09-08.md`](experiment-data-manifest-2026-09-08.md) 以及 SQLite/媒体账本是否仍有引用。

## 本次归档

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 旧实验分支进展快照 | `experiments/experiment-branches-progress-2026-08.html` | 页面引用了已不在当前树中的分支实验文件 |
| Reasonix 工具调用观察 | `experiments/reasonix-typical-tool-call.md` | 外部工具行为记录，不是 Mutsumi 运行时契约 |
| 原型和报告渲染数据 | `data/archive/2026-09-08/` | 生成数据库、日志、脚本和截图，详见数据清单 |
| 上下文实验提交 | Git 标签 `archive/context-v2-experiment-2026-09-08`、`archive/context-v3-experiment-2026-09-08` | 分支已标记归档；本地分支名待清理批准，提交历史仍可检索 |
| Live Lab 提交 | Git 标签 `archive/live-lab-prompt-bench-2026-09-08` | 没有成为当前实验入口；分支已标记归档，待清理批准 |

本目录中的旧设计 HTML 保持在原路径，以免破坏 PR 和相互引用；页面已经加上历史说明。完整处理记录见 [`repository-cleanup-and-experiment-plan-2026-09-08.md`](../repository-cleanup-and-experiment-plan-2026-09-08.md)。
