# Current Context, Heartbeat, And Vision Rules

## Global Event Ledger Rules

- `events` is the append-first global Life Stream. It carries actor, conversation, visibility, audience, lifecycle, turn, tool, and media provenance.
- The current projector presents an audience-filtered unified timeline chronologically: the current conversation and `global` events are model-visible, while unrelated private conversation text remains ledger-only. All human/service sources use provider `user`; readable actor/conversation prefixes preserve identity.
- External service reports use the NapCat-shaped loopback ingress, but `user_id` maps to `service:<id>` and never to a QQ actor. Replies route to configured `ingress.target_user_id`.
- A group has one shared conversation window while members retain independent actor identities. `actor_profile` maintains global aliases and relationship labels.
- Episodes summarize only finalized events and store exact sequence coverage. Raw events are never deleted, and a projection selects an Episode or its covered raw events, never both.
- Each top-level pipeline captures one Ledger high-water snapshot in `PipelineDeps`; initial projection, token reclaim, and context rebuilds use that snapshot. Once a conversation has Ledger rows, legacy `messages/summaries` data is read-only compatibility data and normal cleanup must not write it.
- Media is pipeline-native. SHA-derived media IDs and descriptions are recorded automatically; inbound image URLs are downloaded when possible and otherwise retained as external references. Only the description and media ID enter model context; `sticker_search` and `sticker_manage` maintain the global media ledger.
- Ordinary assistant content must pass the flat-text output gate. Complex Markdown goes through `send.markdown_image`.

- LLM requests use a provider-native non-empty Chinese `system` message for durable platform rules.
- The `persona` prompt and all runtime, message-summary, summary-merge, and Episode-summary prompts live in `system-prompts.yaml`, selected by `prompts.system_file`. Do not duplicate prompt bodies in Python. Production uses `/opt/mutsumi-sync-v3/shared/system-prompts.yaml`.
- Distinct bot and owner identities are mandatory in `identity` (`bot_user_id`, `bot_nickname`, `owner_user_id`, `owner_nickname`, `owner_alias`). Prompt placeholders are replaced once during loading; current actor identity still comes from event metadata, never from nickname matching.
- The provider-native `system` message contains stable platform rules and persona. The following Life Stream uses native `user`, `assistant`, and `tool` messages; tool rounds are reconstructed from `payload_json` and `turn_id`.
- A temporary platform-state user message is inserted immediately before the current source message. It carries current UTC+8 time, runtime flags and active work; source identity appears only in the current message prefix. It is not user-authored chat and must not be written to durable history.
- Summaries, self-note entries, and historical `user` turns must use readable UTC+8 timestamps. Historical `assistant` turns must not receive synthetic timestamp prefixes. Existing self-note lines without timestamps are injected with `很久之前`.
- `bot_state` and `priority_override` are retired model capabilities. Their legacy SQLite tables may remain until production data reset, but they must not be registered or injected.
- Text pipelines save the inbound message before LLM/tool work. If cancelled, the record must be updated to `status=cancelled`; do not allow interrupted messages to disappear silently.
- Pipe-based reply splitting is disabled. Assistant content is sent once and `|` remains literal.
- `send(markdown_image=...)` records verified success/failure in the structured action ledger. Successful artifacts carry the temporary file reference, message id, Markdown hash, and durable Media Ledger ID; the renderer PNG is deleted after ledger registration and artifact markers must not enter assistant history.
- Memory writes remain staged until cancellation-protected cleanup. Immediate tool feedback must say `staged`, and each final operation is committed and ledgered exactly once.
- Historical native tool projection must use the final committed memory-write result, not the provisional `staged` feedback.
- Malformed final envelopes receive bounded correction. After exhaustion, send only an unambiguous complete `TO_USER` block or a flat-text protocol failure; never silently lose an ordinary user turn and never salvage `TO_SELF`.
- Event Ledger 历史只由带精确 sequence coverage 的 Episode 替代原始事件；旧 `compaction`/`last_message_id` 只服务于无 Ledger 的兼容数据库，不能跳过 Ledger 记录。
- Heartbeat scans real inbound activity every 15 minutes for private conversations and every 3 hours for groups active within the last 24 hours.
- Heartbeat uses `remember_input=False` but may persist verified assistant output and update the target window; it never sends cold-session pokes or status updates and yields to user pipelines.
- Heartbeat configuration is `heartbeat.private_interval_seconds=900`, `group_interval_seconds=10800`, and `active_window_seconds=86400`.
- `scheduler` is a built-in durable scheduling tool. It supports one-shot, fixed-interval, and five-field cron wake-ups plus `list/pause/resume/cancel`; tasks store time, prompt, an optional opaque natural-language `context`, and the conversation bound at registration.
- Image recognition is provided through the optional `vision` provider config. Supported providers are `openai-compatible` and `volcengine-ocr`; Volcengine OCR requires AK/SK and can also sign an optional `session_token`. Do not bind image input to the main DeepSeek text model unless that provider explicitly supports images.
- Production logging uses the standard `mutsumi.*` logger tree and also writes append-only NDJSON stream records to `logging.stream_store.path` plus human-readable rotating text records to `logging.text_file.path`. Do not bypass standard logging for pipeline diagnostics.
- The optional ingress enforces a loopback host and positive numeric owner QQ target, requires a Bearer token, accepts only text and HTTP(S) image URLs, persists accepted events as `received`, deduplicates by service `user_id` plus `message_id`, and restores unfinished service events after restart or graceful cancellation.

## AI-Native Workflow Design Rules

- 所有后续设计首先遵循两个原则：选择能够闭环的最小通用机制，并尽量把判断、规划和执行空间留给 AI。只有真实需求证明现有机制不完备时，才增加新的结构和抽象。
- 工作流由自然语言 prompt 串联。后端负责时间、进程、权限、持久化、回调和可观察性，不把论文、行情、日程等业务步骤固化成 connector、workflow DSL、step 列表或领域 JSON。
- 定时任务的完整语义是“在约定时间向注册时绑定的会话 pipeline 注入一段 prompt，可附带一段不由 Scheduler 解释的自然语言 context”。当前支持一次性、固定间隔和通用五字段 cron；Scheduler 的扩充只能围绕时间触发与任务生命周期。
- Scheduler 不接受 `skill_name`、`target_group`、connector 类型、业务步骤或领域参数。目标会话由调用现场绑定；Skill 路径、任务目的和交付要求都写进 prompt。
- Skill 只是按需读取的文本文件，不建立注册中心、解释器、状态机或专用运行时。需要渐进式披露时，可以约定短的 bot 指引和完整的 worker 指引两层文件，但该约定不进入 Scheduler 数据模型。
- 主 bot 通过一个通用委派工具把自然语言任务交给 OpenCode 等外部任务 Agent。委派参数保持最少；工作指引路径同样写在 prompt 中，由外部 Agent 自己读取、规划并使用其通用工具执行。
- 外部 Agent 必须自觉调用受控的 `report_task` 工具报告完成结果。运行实例、原任务和原会话由平台在服务端绑定；报告工具只接收自然语言结果，不让模型填写目标用户或群聊。
- 不把外部 Agent 的 stdout 或进程退出当作成功报告，也不由后端猜测并补写结果。未调用 `report_task` 就保持未报告或超时状态，并诚实记录；不得伪造完成。
- 外部 Agent 的完整工作上下文不进入主 Life Stream。主 bot 只接收经过平台验证的任务状态和紧凑自然语言报告，再由原 pipeline 决定如何记忆和向用户表达。
- Tool schema 中必要的结构只用于通用传输、鉴权和生命周期控制，不用于预先编排 AI 的业务思考。不要因为某个单一工作流方便，就向全局架构增加专用字段。

最小心智模型：`定时器 = 时间 + prompt`，`Skill = 文件`，`OpenCode = 通用任务 Agent`，`report_task = 回调`，`Bot = 人格、关系、上下文与最终交流`。

# AGENTS.md - AI Agent 协作指南

## 项目概况

Mutsumi's SYNC v3 是一个基于 NapCat QQ 的异步聊天机器人。v3 从旧版代码评估后重写，核心目标是：用清晰的异步调度器、单函数 Pipeline、可热更新工具系统、长期记忆与可观察 TUI，把机器人做成可维护的个人 Agent。

当前主线已经具备：

| 能力 | 状态 |
| --- | --- |
| NapCat WebSocket/HTTP I/O | 可用 |
| 本地认证外部服务 ingress | 可用，默认关闭，NapCat-shaped `POST /v1/events` |
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
| `scheduler` 工具 | 可用，持久化一次性/固定间隔/cron 定时触发 pipeline，并支持生命周期管理 |
| `file_edit` 工具 | 可用，受控 workspace 内的 list/read/write/patch |
| Skill 文件 | 可用，按需读取的 BOT.md 与 OpenCode SKILL.md 约定 |
| OpenCode 委派 | 可选，显式 `report_task` 回报后才唤醒原会话 |
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
- 当前会话的普通文字不要调用 `send` 工具，应写入最终 envelope 的 `TO_USER`。跨会话互动可以用 `send.text` 配合明确不同的 target 发送纯文本；其他 `send` 调用用于 `media_id`、`markdown_image`、图片、表情、@、回复、转发等特殊消息段。`send.target` 必填且只能使用 `private:<数字>` 或 `group:<数字>`，不得使用昵称或猜测 ID。复用 Media Ledger 内容时先搜索再把真实 `media_id` 传给 `send`，不得猜测路径。
- 预计较慢的工具前调用 `status_update`；它不结束 pipeline，也不进入 assistant history。未调用时 long tool 自动获得一次兜底通知；heartbeat 禁用状态通知。
- 只有确实没有可见内容的静默维护或自主检查才调用 `no_reply`；未知事实、拒答、隐私边界说明和工具完成结果仍必须写入 `TO_USER`，不要用它逃避普通回答。

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
14. 有界 Ledger 投影若未包含更早的未摘要历史，必须写入明确的 omission 标记，不能把分页上限伪装成完整覆盖。

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
| `src/mutsumi_sync/ingress.py` | 本地 HTTP ingress、token 鉴权与服务事件落库 |
| `src/mutsumi_sync/scheduler.py` | 调度器、状态持有者 |
| `src/mutsumi_sync/pipeline.py` | 单函数消息处理核心 |
| `src/mutsumi_sync/config.py` | Pydantic 配置与 YAML 保存 |
| `src/mutsumi_sync/memory/store.py` | SQLite 长期记忆 |
| `src/mutsumi_sync/memory/projection.py` | Event Ledger 到 Life Stream 的一致性投影与完整 pipeline 边界 |
| `src/mutsumi_sync/memory/episodes.py` | finalized pipeline 的 Episode 压缩 |
| `src/mutsumi_sync/opencode.py` | OpenCode 进程、临时插件与回报接收 |
| `src/mutsumi_sync/tools/file_edit.py` | 受控文件编辑工具 |
| `skills/` | 按需披露的 Skill 文本文件 |
| `src/mutsumi_sync/tools/send.py` | send 工具 |
| `src/mutsumi_sync/tools/no_reply.py` | 静默回复控制工具 |
| `src/mutsumi_sync/tools/markdown_renderer.py` | Python 调 Node renderer |
| `tools/markdown-renderer/` | Markdown -> PNG Node renderer |
| `src/mutsumi_sync/tui/dashboard.py` | Dashboard TUI |
| `src/mutsumi_sync/tui/tester.py` | 交互式测试器 |
