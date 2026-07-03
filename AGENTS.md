# AGENTS.md - AI Agent 协作指南

## 项目概况

Mutsumi's SYNC v3 是一个基于 NapCat QQ 的异步聊天机器人。v3 从旧版代码评估后重写，核心目标是：用清晰的异步调度器、单函数 Pipeline、可热更新工具系统、长期记忆与可观察 TUI，把机器人做成可维护的个人 Agent。

当前主线已经具备：

| 能力 | 状态 |
| --- | --- |
| NapCat WebSocket/HTTP I/O | 可用 |
| `PipelineScheduler` 异步调度 | 可用，每个会话 key 一个 cancellable task |
| 单函数 `pipeline()` | 可用，所有处理逻辑集中在一个异步函数 |
| OpenAI-compatible LLM 调用 | 可用，支持 DeepSeek reasoning_content |
| 工具循环 | 可用，支持 registry version 热更新 |
| SQLite 消息/摘要/自我印象存储 | 可用 |
| 上下文拼接与窗口回收 | 可用，CONTEXT 日志不截断 |
| Dashboard TUI | 可用，支持彩色日志、滚动、选择复制、命令历史 |
| 交互式 tester | 可用，支持 `/inject`、`/break`、FakeSender |
| 输出协议 | assistant `content` 是用户可见回复，未转义 `|` 分成多条 QQ 消息 |
| `no_reply` 工具 | 可用，用于本轮故意静默 |
| `send` 工具 | 特殊发送与兼容工具，支持 text/image/image_url/face/at/reply/forward/markdown_image |
| Markdown 图片渲染 | 可选能力，Node + Playwright 渲染 Markdown/LaTeX/code/Mermaid 为 PNG |

## 必须先读

开始任何代码工作前，按顺序阅读：

1. `README.md` - 面向用户和维护者的当前说明。
2. `init.md` - 当前项目章程与架构约束。
3. `bottle/docs/architecture-for-humans.md` - 原始人类版架构设计书。
4. `bottle/docs/architecture-for-ai.md` - 原始结构化架构设计书。

`bottle/docs/` 是 v3 重写的设计来源；如果它与当前代码冲突，以 `README.md`、`init.md` 和源码测试为准。

## 初次运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item config.example.yaml config.yaml
# 编辑 config.yaml，填入 NapCat 与 LLM 配置

$env:PYTHONPATH = "."
python -m src.mutsumi_sync.main
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
PYTHONPATH=. python -m src.mutsumi_sync.main
```

## 开发入口

```powershell
# 交互式测试器，无需真实 NapCat
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.tester

# Dashboard TUI
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.dashboard config.yaml
```

Dashboard 常用命令：

| 命令 | 作用 |
| --- | --- |
| `/inject private <uid> <msg>` | 注入私聊消息 |
| `/inject group <gid> <uid> <msg>` | 注入群聊消息 |
| `/break private <uid>` | 取消指定 pipeline |
| `/watch <key>` | 固定观察一个实例 |
| `/auto` | 自动跟随最新实例 |
| `/memory [key]` | 查看当前记忆快照 |
| `/config <key> [value]` | 读取或局部修改配置 |

## Markdown 图片渲染

普通文字回复应直接写 assistant `content`。需要发送富文本图片时，`send` 工具支持：

```json
{
  "markdown_image": "# 标题\n\n$$E=mc^2$$\n\n```mermaid\ngraph TD; A-->B\n```"
}
```

启用方式：

```powershell
.\scripts\install_markdown_renderer.ps1
```

Linux:

```bash
sh scripts/install_markdown_renderer.sh
```

然后在 `config.yaml` 中打开：

```yaml
render:
  markdown_image:
    enabled: true
```

Linux 若 Chromium 缺系统依赖，按安装脚本提示执行：

```bash
cd tools/markdown-renderer
npx playwright install-deps chromium
```

## LLM 输出协议

- 最终轮 assistant `content` 会发送给用户；reasoning_content 永远不发送。
- 如果要分多条 QQ 消息，使用未转义的 `|` 分隔；正文里的字面量竖线写成 `\|`。
- 有 `tool_calls` 的轮次只执行工具并回填结果，中间 content 不发送；没有工具的最终 content 才发送。
- 普通文字不要调用 `send` 工具。`send` 只用于 `markdown_image`、图片、表情、@、回复、转发等特殊消息段，或旧兼容路径。
- 本轮不应回复时调用 `no_reply`，并保持 content 为空。

## 运行测试

```powershell
$env:PYTHONPATH = "."
python -m pytest tests/ -q
```

可选的 Node renderer 检查：

```powershell
cd tools/markdown-renderer
npm run check
```

## Git 约定

- 主线分支：`main`。
- v3 集成分支：`feature/v3-rewrite`。
- 历史 v2 存档：`archive/legacy` tag。
- 提交消息：中文，使用约定式提交前缀，如 `feat:`、`fix:`、`docs:`、`test:`、`refactor:`。
- 不提交：`config.yaml`、`.env`、`data/`、`node_modules/`。

## 架构铁律

以下规则不可违反：

1. `pipeline()` 是单个异步函数，不拆成类方法链或回调链。
2. Pipeline 不持有状态，所有状态由 Scheduler 或注入依赖持有。
3. 全局工具注册表变更用 `registry.version` 单调计数器追踪，不用跨 pipeline 共享 bool 脏标记。
4. 取消使用 `asyncio.Task.cancel()`，不自造取消协议。
5. 工具错误返回 `"[Error: ...]"` 字符串，不把异常漏给 LLM。
6. Skill/Tool 加载不应在导入期执行不受控副作用。
7. 配置修改工具必须尽量局部修改 YAML，不应整份重排用户配置文件。
8. 日志链路要诚实打印 pipeline 所有关键分支，不用 UI 滚动状态掩盖日志缺失。

## 代码约定

- 所有函数签名写类型注解。
- I/O 使用 async/await；纯计算保持同步函数。
- 日志使用 `logging.getLogger("mutsumi.xxx")`。
- 导入放顶部，除非循环依赖不可避免。
- ANSI 颜色码只放在 TUI/tester/pipeline 的格式化输出边界。
- 编辑文件时尊重现有脏工作树，不回滚他人改动。

## 添加新 Tool

```python
async def my_tool(args: dict, *, config: Config, sender: MessageSender, **deps) -> str:
    """Tool description used by LLM function schema."""
    return "result"
```

注册：

```python
registry.register(Tool(
    name="my_tool",
    description="...",
    parameters={...},
    handler=my_tool,
    source="builtin",
))
```

## 重要文件

| 文件 | 说明 |
| --- | --- |
| `src/mutsumi_sync/main.py` | 真实入口与工具注册 |
| `src/mutsumi_sync/scheduler.py` | 调度器、状态持有者 |
| `src/mutsumi_sync/pipeline.py` | 单函数消息处理核心 |
| `src/mutsumi_sync/config.py` | Pydantic 配置与 YAML 保存 |
| `src/mutsumi_sync/memory/store.py` | SQLite 长期记忆 |
| `src/mutsumi_sync/tools/send.py` | send 工具 |
| `src/mutsumi_sync/tools/no_reply.py` | 静默回复控制工具 |
| `src/mutsumi_sync/tools/markdown_renderer.py` | Python 调 Node renderer |
| `tools/markdown-renderer/` | Markdown -> PNG Node renderer |
| `src/mutsumi_sync/tui/dashboard.py` | Dashboard TUI |
| `src/mutsumi_sync/tui/tester.py` | 交互式测试器 |
