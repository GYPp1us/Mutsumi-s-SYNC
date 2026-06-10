# AGENTS.md — AI Agent 协作指南

## 项目概况

Mutsumi's SYNC v3 — QQ 聊天机器人。从 v2 代码库评估后完全重写。
当前分支 `feature/v3-rewrite` 是 orphan 分支，只有漂流瓶 (`bottle/`) 和项目文件。

## 必须先读

开始任何工作前，按顺序读：

1. `init.md` — 项目章程（技术栈、模块、阶段）
2. `bottle/docs/architecture-for-humans.md` — 架构设计书
3. `bottle/docs/architecture-for-ai.md` — 结构化架构（供 AI 精确理解）

## 运行命令

```bash
# 启动（Phase 1 完成后）
python -m src.mutsumi_sync.main

# 运行测试
python -m pytest tests/ -v

# 类型检查
# (待配置)

# 代码风格
# (待配置)
```

## Git 约定

- 当前分支: `feature/v3-rewrite`
- v2 存档: `archive/legacy` tag
- 提交消息: 英文，约定式提交风格（`feat:`, `fix:`, `refactor:`, `test:`, `docs:`）
- **绝不提交**: `config.yaml`（已被 `.gitignore`）

## 架构铁律

以下规则不可违反，违反即视为 bug：

1. **Pipeline 是单个异步函数** — 不拆成多个类方法或回调链。所有处理逻辑在 `pipeline()` 内。
2. **Pipeline 不持有状态** — 所有依赖通过参数注入。状态由 Scheduler 持有。
3. **工具修改全局状态 → 版本号递增** — 不用布尔脏标记。用 `registry.version` 单调计数器。
4. **取消 = `Task.cancel()`** — 不发明自定义取消协议。
5. **技能加载不执行代码** — Skill 定义是数据。

## 代码约定

- 类型注解：所有函数签名必须有完整类型注解
- 异步：I/O 操作用 async/await；纯计算用同步函数
- 错误：工具错误返回 `"[Error: ...]"` 字符串，不抛异常
- 日志：`logging.getLogger("mutsumi.xxx")`，每个模块独立 logger
- 导入：顶部导入，不懒加载（除非循环依赖不可避免）
- 依赖注入：函数参数 `*, deps`，不用全局单例

## 添加新 Tool

```python
# tools/my_tool.py
async def my_tool(args: dict, *, config: Config, sender: MessageSender, **deps) -> str:
    """Tool description (used as LLM function description)."""
    # 只做自己的事，不操心 Pipeline 怎么用结果
    return "result string"

# 在 Scheduler 初始化时注册：
registry.register(Tool(
    name="my_tool",
    description="...",
    parameters={...},  # JSON Schema
    handler=my_tool,
    source="builtin",
))
```

## 添加 Skill

```python
# skills/my_skill.py
def register(registry: ToolRegistry) -> None:
    registry.register(Tool(...))
    registry.register(Tool(...))

def system_prompt() -> str:
    return "提示词片段..."
```

## 参考

- NapCat API: [napcat-api.md](https://github.com/NapCatQQ/NapCatQQ) （项目 `bottle/docs/` 中无此文件，见 v2 存档 `PROMPT/napcat-api.md`）
- Python asyncio Task: <https://docs.python.org/3/library/asyncio-task.html>
