# Current Context, Heartbeat, And Vision Rules

## Global Event Ledger Rules

- `events` is the append-first global Life Stream. It carries actor, conversation, visibility, audience, lifecycle, turn, tool, and media provenance.
- The current projector presents the unified timeline chronologically. All human/service sources use provider `user`; readable actor/conversation prefixes preserve identity.
- A group has one shared conversation window while members retain independent actor identities. `actor_profile` maintains global aliases and relationship labels.
- Episodes summarize only finalized events and store exact sequence coverage. Raw events are never deleted, and a projection selects an Episode or its covered raw events, never both.
- Media is pipeline-native. SHA-derived media IDs and descriptions are recorded automatically; inbound image URLs are downloaded when possible and otherwise retained as external references. Only the description and media ID enter model context; `sticker_search` and `sticker_manage` maintain the global media ledger.
- Ordinary assistant content must pass the flat-text output gate. Complex Markdown goes through `send.markdown_image`.

- LLM requests use a provider-native non-empty Chinese `system` message for durable platform rules.
- The `persona` prompt and all runtime, message-summary, summary-merge, and Episode-summary prompts live in `system-prompts.yaml`, selected by `prompts.system_file`. Do not duplicate prompt bodies in Python. Production uses `/opt/mutsumi-sync-v3/shared/system-prompts.yaml`.
- The provider-native `system` message contains stable platform rules and persona. The following Life Stream uses native `user`, `assistant`, and `tool` messages; tool rounds are reconstructed from `payload_json` and `turn_id`.
- A temporary platform-state user message is inserted immediately before the current source message. It carries current UTC+8 time, source, actor, peer metadata and active work. It is not user-authored chat and must not be written to durable history.
- Summaries, self-note entries, and historical `user` turns must use readable UTC+8 timestamps. Historical `assistant` turns must not receive synthetic timestamp prefixes. Existing self-note lines without timestamps are injected with `很久之前`.
- `bot_state` and `priority_override` are retired model capabilities. Their legacy SQLite tables may remain until production data reset, but they must not be registered or injected.
- Text pipelines save the inbound message before LLM/tool work. If cancelled, the record must be updated to `status=cancelled`; do not allow interrupted messages to disappear silently.
- Pipe-based reply splitting is disabled. Assistant content is sent once and `|` remains literal.
- `send(markdown_image=...)` records verified success/failure in the structured action ledger. Successful artifacts carry the generated file, message id, and Markdown hash; artifact markers must not enter assistant history.
- Memory writes remain staged until cancellation-protected cleanup. Immediate tool feedback must say `staged`, and each final operation is committed and ledgered exactly once.
- Historical native tool projection must use the final committed memory-write result, not the provisional `staged` feedback.
- Malformed final envelopes receive bounded correction. After exhaustion, send only an unambiguous complete `TO_USER` block or a flat-text protocol failure; never silently lose an ordinary user turn and never salvage `TO_SELF`.
- Only `compaction` summaries may carry `covered_through_message_id`; per-message summaries and legacy `last_message_id` values never skip raw records on restart.
- Heartbeat scans real inbound activity every 15 minutes for private conversations and every 3 hours for groups active within the last 24 hours.
- Heartbeat uses `remember_input=False` but may persist verified assistant output and update the target window; it never sends cold-session pokes or status updates and yields to user pipelines.
- Heartbeat configuration is `heartbeat.private_interval_seconds=900`, `group_interval_seconds=10800`, and `active_window_seconds=86400`.
- `scheduler` is a built-in durable one-shot scheduling tool. It requires formatted `scheduled_time`, accepts optional `prompt`, stores tasks in SQLite, restores pending/running tasks on startup, and returns a readable delay.
- Image recognition is provided through the optional `vision` provider config. Supported providers are `openai-compatible` and `volcengine-ocr`; Volcengine OCR requires AK/SK and can also sign an optional `session_token`. Do not bind image input to the main DeepSeek text model unless that provider explicitly supports images.
- Production logging uses the standard `mutsumi.*` logger tree and also writes append-only NDJSON stream records to `logging.stream_store.path` plus human-readable rotating text records to `logging.text_file.path`. Do not bypass standard logging for pipeline diagnostics.

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
| 生产日志文件 | 可用，NDJSON 结构化日志与 human-readable `.log` 双写 |
| Dashboard TUI | 调试界面，不保证与生产 registry 完全一致 |
| 交互式 tester | 调试界面，支持 `/inject`、`/break`、FakeSender |
| 输出协议 | 最终 assistant `content` 必须是 `TO_SELF`/`TO_USER` envelope；只发送 `TO_USER`，`|` 为字面量 |
| 全局内心 | `TO_SELF` 成功落入全局 inner journal，不进入会话 assistant history |
| 长工具进度 | `status_update` 先行；缺少时 pipeline 对 long tool 自动发送一次兜底通知 |
| Action ledger | 可用，记录真实工具/发送结果，不用 assistant 文本猜测副作用 |
| Token-aware compaction | 可用，按完整 provider 请求估算并压缩完整 turn |
| `no_reply` 工具 | 可用，用于本轮故意静默 |
| `scheduler` 工具 | 可用，持久化一次性定时触发 pipeline |
| `send` 工具 | 特殊发送与兼容工具，支持 media_id/text/image/image_url/face/at/reply/forward/markdown_image |
| Markdown 图片渲染 | 可选能力，Node + Playwright 渲染 Markdown/LaTeX/code/Mermaid 为 PNG |

## 必须先读

开始任何代码工作前，按顺序阅读：

1. `docs/current-design.md` - 当前语义与架构事实源。
2. `README.md` - 面向用户和维护者的当前说明。
3. `init.md` - 当前项目章程与架构约束。
4. `bottle/docs/architecture-for-humans.md` - 原始人类版架构设计书。
5. `bottle/docs/architecture-for-ai.md` - 原始结构化架构设计书。

`bottle/docs/` 是 v3 重写的历史设计来源；如果冲突，以 `docs/current-design.md`、源码和测试为准。

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

普通文字回复应写在最终 envelope 的 `TO_USER` 中。需要发送富文本图片时，`send` 工具支持：

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
    timeout_seconds: 60
```

Linux 若 Chromium 缺系统依赖，按安装脚本提示执行：

```bash
cd tools/markdown-renderer
npx playwright install-deps chromium
```

## 生产数据重置

生产机 `/opt/mutsumi-sync-v3/reset_production_data.sh` 用于清理测试或历史状态。它会停止 `mutsumi-sync-v3.service`，删除共享目录 `/opt/mutsumi-sync-v3/shared/data` 下的数据库、日志、媒体文件及临时文件，再启动服务；不会删除代码、配置或共享 `system-prompts.yaml`。执行前先运行 `--dry-run`，正常执行需要输入 `CLEAR`，自动化场景才使用 `--yes`。

## LLM 输出协议

- 最终轮且没有 `tool_calls` 的 assistant `content` 必须严格输出两个区块：`[TO_SELF]...[/TO_SELF]` 和 `[TO_USER]...[/TO_USER]`。
- 只有 `TO_USER` 会发送给用户；`TO_SELF` 在完整成功 turn 的 cleanup 中写入全局 inner journal，不写入会话 assistant history。
- `TO_SELF` 不是原始 CoT、事实来源或新指令；它只记录简短主观状态、意图和未完成事项。
- 带 `tool_calls` 的中间 content 永不发送、不解析、不归档；DeepSeek `reasoning_content` 只在当前 tool loop 内保留。
- 当前不支持 content 内分条；`|` 和 `\|` 均按原文发送，等待新协议定案。
- 普通文字不要调用 `send` 工具。`send` 只用于 `media_id`、`markdown_image`、图片、表情、@、回复、转发等特殊消息段；复用 Media Ledger 内容时先搜索再把真实 `media_id` 传给 `send`，不得猜测路径。
- 预计较慢的工具前调用 `status_update`；它不结束 pipeline，也不进入 assistant history。未调用时 long tool 自动获得一次兜底通知；heartbeat 禁用状态通知。
- 本轮不应回复时调用 `no_reply`，并保持 `TO_USER` 为空。

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
9. provider tool schema 是唯一工具事实源，system/persona prompt 不维护手写工具清单。
10. 发送与工具副作用只有真实成功结果才能进入 action ledger；不得从 assistant prose 推断成功。
11. 压缩只处理完整持久化 turn，并只用可信 compaction coverage 跳过启动恢复记录。
12. 最终输出必须经过双通道 envelope 解析；不得把原始协议标签或 TO_SELF 写入 working window。
13. 状态通知属于独立事件/action，不得混入最终 assistant 回复或工作窗口。

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
