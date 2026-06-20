# AGENTS.md — AI Agent 协作指南

## 项目概况

Mutsumi's SYNC v3 — QQ 聊天机器人。从 v2 代码库评估后完全重写。
当前 Phase 1 已完成：异步调度系统 + NapCat I/O 层 + 配置 + 工具注册表 + 交互式测试器。
Pipeline 内 LLM 调用逻辑为 Phase 1 stub（留待后续实现）。

### 已实现的特性

| 特性 | 说明 |
|------|------|
| **SQLite 消息存储** | `memory/store.py` — 按日期/消息组/类别筛选，二进制媒体文件存到 `data/media/` |
| **思考模式** | DeepSeek thinking mode + `reasoning_effort` 控制（默认 max） |
| **格式化输出块** | LLM 回应和 SEND 操作均以 `=====[...]=====` 框包裹，分隔线白色、内容灰色 |
| **消息段渲染** | `text`/`image`/`face`/`at`/`reply`/`record`/`video`/`forward` 自动解析 |
| **交互式测试器** | 无 NapCat 可 `/inject` 模拟消息，`/break` 打断 pipeline，实时彩色日志 |
| **异常防护** | 所有 `create_task` 有包装，stdin 线程覆盖 OSError，日志队列设 asctime |

## 必须先读

开始任何工作前，按顺序读：

1. `init.md` — 项目章程（技术栈、模块、阶段）
2. `bottle/docs/architecture-for-humans.md` — 架构设计书
3. `bottle/docs/architecture-for-ai.md` — 结构化架构（供 AI 精确理解）

## 初次运行

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1    # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt

# 2. 创建配置文件（config.yaml 被 gitignored）
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入 NapCat 连接信息和 LLM API Key

# 3. 启动
$env:PYTHONPATH = "."; python -m src.mutsumi_sync.main

# 或启动交互式测试器（不需 NapCat，可手动 /inject 消息）
$env:PYTHONPATH = "."; python -m src.mutsumi_sync.tui.tester
```

> 无需 API Key 时 pipeline 自动降级为本地 stub：`[LLM Stub @ timestamp] I received: ...`

## 运行测试

```bash
$env:PYTHONPATH = "."; python -m pytest tests/ -v
```

测试需要 `PYTHONPATH` 设置为项目根目录（当前无 `pyproject.toml` 可编辑安装）。

## Git 约定

- 当前分支: `feature/v3-rewrite`
- v2 存档: `archive/legacy` tag
- 提交消息: 中文，约定式提交风格（`feat:`, `fix:`, `refactor:`, `test:`, `docs:`）
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
- ANSI 颜色码：仅限 `tui/tester.py` 和 `pipeline.py` 的格式化块

## LLM 输出格式

```python
# pipeline.py — _log_llm_result
=========[provider][model]=========
[reasoning]                        # 存在时灰色显示
<思考链内容>                         # 灰色
[/reasoning]                       # 灰色
<回答内容>                           # 灰色
=========[↑:input_tokens][↓:output_tokens]=========

# 思考模式控制（config.yaml）
model.reasoning_effort: "max"      # 可选: high / max
```

## 消息发送格式

```python
# tui/tester.py — _FakeSender
=========[SEND][private/group][peer_uid]=========
  [text] hello world                # 灰色
  [image] file=...                  # 灰色
=========[1 segment(s)]=========

# 支持的消息段: text, image, face, at, reply, record, video, forward
# _render_segment() 可扩展
```

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

- DeepSeek API: `https://api-docs.deepseek.com/zh-cn/api/create-chat-completion`
- NapCat API: `bottle/docs/napcat-api.md`
- Python asyncio Task: <https://docs.python.org/3/library/asyncio-task.html>
