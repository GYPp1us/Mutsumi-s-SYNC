# V3 代码与实验归档

`v3/` 原样保留 2026-10-18 冻结的 Python 实现、独立原型、测试、实验源码和设计资料。该目录不是 V4 当前实现；其历史文档和内部 `AGENTS.md` 在归档范围内适用。

## Git 恢复入口

| 归档对象 | 引用 / 提交 |
| --- | --- |
| 完整 V3 工作树快照 | 分支 `archive/v3`；提交 `a57cea494dfd7ef9b0692da5fd233f7a6fedb4a9` |
| V3 冻结标签 | `archive/v3-final-2026-10-18` |
| 本地 main 独立提交 | `archive/v3-local-main-2026-10-18` → `732cd9e2e6a7d30a753ab4149b84f5e88c073f36` |
| 远端 V3 集成提交 | `archive/v3-remote-integration-2026-10-18` → `e3cccf286722ebecd44f49142f7424b52b883711` |
| Context v2 实验 | `archive/context-v2-experiment-2026-09-08` → `72d2db1` |
| Context v3 实验 | `archive/context-v3-experiment-2026-09-08` → `3d97284` |
| Live Lab 实验 | `archive/live-lab-prompt-bench-2026-09-08` → `53c4eb2` |
| 旧版重写与 Markdown 工作 | `archive/feature-v3-rewrite-2026-09-08`、`archive/feature-markdown-image-send-2026-09-08` |

其他旧分支、标签与工作树继续保留；本次不删除其引用。远端发布尚待完成，这些归档引用当前以本地 Git 为准。

## 本地完整恢复包

目录：`.local-archives/v3-20261018T115559/`，受根目录 `.gitignore` 忽略。

- `all-refs-before.bundle`：变更前的全部 Git 引用与提交。
- `v3-archive.bundle`：包含最终 V3 快照和归档标签的完整 bundle，已通过 `git bundle verify`。
- `working-tree.zip`：134 个项目文件的完整工作树快照，逐文件哈希已核对。
- `experiment-data.zip`：11 个已登记的实验生成文件，逐文件哈希已核对；原文件继续位于 `data/archive/2026-09-08/`。
- `tracked-changes.patch`、`index-before`、清单与校验信息：保存原未提交内容和索引状态。

bundle 可以克隆为单独仓库，ZIP 用于恢复未提交内容和实验数据。不要把包含本机资料的恢复包加入公开仓库。

## HZ 生产旧版

服务器归档位于 `/opt/mutsumi-sync-archives/legacy-v3-20261018T113524+0800/`，其中包含代码、环境、配置、提示词和恢复说明。旧服务已经停止且禁用自启；详情见 [本轮记录](../docs/refactor-decisions-2026-10-18.md)。
