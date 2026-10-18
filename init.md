# Mutsumi's SYNC v3 - 项目章程

**状态**: v3 主线可用，继续迭代中  
**主线分支**: `main`  
**v3 集成分支**: `feature/v3-rewrite`  
**旧版存档**: `archive/legacy` tag

独立实验入口：`python -m src.mutsumi_sync.prototype --demo`。详见[聊天与后台工作原型说明](docs/prototype-conversation-background-work.md)。原型以独立数据库和本地终端验证文本聊天、后台只读任务与消息列表，尚未接入生产 QQ 处理流程；下文主线能力与约束保持原义。

仓库清理、历史资料归档、分支状态和新编排实验接手说明见[2026-09-08 清理与实验报告](docs/repository-cleanup-and-experiment-plan-2026-09-08.md)。

---

## 1. 项目定位

Mutsumi's SYNC v3 是一个基于 NapCat 的 QQ 聊天机器人。它不是简单的回复脚本，而是一个可以接入 OpenAI-compatible LLM、调用工具、维护长期记忆、观察 pipeline 状态、并由 TUI 辅助调试的个人 Agent 运行时。

核心设计取向：

- 后续重构以模型可自由组合行动的能力为先：收发对象、多方多轮交流、等待与多设备协作由模型编排，不为提高准确率而预先封死任务路径。准确率用于观察和减少幻觉，不能以任务能力下降换取数字改善。
- 运行时简单明确，避免 LangChain 式重抽象。
- Pipeline 维持单函数，便于 AI agent 和人类维护者一次性理解完整消息处理流程。
- Scheduler 持有状态，Pipeline 只消费依赖。
- 工具系统可热更新，并能在同一次 pipeline 调用中感知变化。
- 本地测试和真实 NapCat 运行路径尽量一致。

## 2. 技术栈

| 组件 | 选型 |
| --- | --- |
| Runtime | Python 3.10+, asyncio |
| QQ I/O | NapCat WebSocket + HTTP API |
| 外部服务 I/O | 可选 loopback HTTP ingress，复用 NapCat message event |
| LLM | OpenAI-compatible HTTP API via `httpx` |
| 配置 | Pydantic + YAML + `.env` |
| 存储 | SQLite via `aiosqlite` |
| TUI | prompt_toolkit |
| Markdown 图片渲染 | Optional Node.js + Playwright + markdown-it + KaTeX + Mermaid |
| 测试 | pytest + pytest-asyncio |

## 3. 当前能力

| 能力 | 当前状态 |
| --- | --- |
| Phase 1 骨架 | 完成 |
| NapCat receiver/sender/classifier | 完成 |
| Config 加载、热修改、局部保存 | 完成 |
| ToolRegistry 与内置工具 | 完成 |
| LLM 调用与 DeepSeek reasoning | 完成 |
| 工具循环 | 完成 |
| SQLite 消息存储 | 完成 |
| 上下文拼接、摘要、自我印象 | 完成 |
| 请求级 token 预算、精确 turn 压缩 | 完成 |
| 结构化 action ledger | 完成 |
| 图片描述进入常规 pipeline | 完成 |
| Global Life Stream 与 Actor Registry | 完成 |
| 外部服务消息 ingress、去重、FIFO 与重启恢复 | 完成 |
| 历史 tool round 原生回放 | 完成 |
| Dashboard TUI | 调试界面，不作为生产 registry 基准 |
| 交互式 tester | 调试界面 |
| Markdown -> PNG -> image send | 完成，可选安装 |

## 4. 架构铁律

1. **Pipeline 是单个异步函数**  
   `src/mutsumi_sync/pipeline.py` 中的 `pipeline()` 是消息处理核心，不拆成多个类方法或回调链。

2. **Pipeline 不持有状态**  
   Config、ToolRegistry、Store、Window、Session、Sender 等由 Scheduler 或调用方持有，通过 `PipelineDeps` 注入。

3. **Scheduler 持有全局与会话状态**  
   全局状态：Config、ToolRegistry、MessageStore、Sender 等。  
   会话状态：MessageWindow、SessionState、正在执行的 Task。

4. **取消使用 `Task.cancel()`**  
   新消息到达同一个 key 时，Scheduler 取消旧 task 并创建新 task。

5. **工具变更用版本号追踪**  
   `ToolRegistry.version` 单调递增，pipeline 通过版本比较刷新工具快照。

6. **工具错误返回字符串**  
   工具 handler 不应把异常直接抛给 LLM，错误以 `"[Error: ...]"` 返回。

7. **配置修改必须局部化**  
   `config_manager set` 修改 YAML 时尽量只更新目标键，不重排整个用户配置。

8. **日志链路要诚实**  
   pipeline 的关键分支、上下文拼接、LLM 结果、保存/窗口更新、cleanup 都应有可追踪日志。

9. **上下文使用统一 Life Stream**
   多个用户和服务来源都进入同一条按事件顺序排列的时间线；provider role 只表达消息类型，actor 与 conversation 由平台来源前缀表达。

10. **历史工具调用必须保持原生结构**
    事件通过 `turn_id` 和 `payload_json` 重建 `assistant(tool_calls)` 与 `tool` 结果，禁止把工具反馈伪装成普通 assistant 文本。

11. **外部服务身份与 QQ 目标必须分离**
    ingress 请求中的 `user_id` 只表示来源服务，映射为 `service:<id>`；模型回复始终通过配置的正整数 QQ `ingress.target_user_id` 发给所有者。监听必须为 loopback，消息只接受文本和 HTTP(S) 图片 URL。外部事件先持久化为 `received`，由 service FIFO worker 处理，重启或关机取消后恢复未完成事件。

## 5. 模块职责

```text
src/mutsumi_sync/
  main.py                 production entry, registry construction
  scheduler.py            PipelineScheduler, task lifecycle, state ownership
  pipeline.py             single async processing function
  config.py               Pydantic config, YAML load/save/reload
  logging.py              logging helpers
  message/
    receiver.py           NapCat WebSocket events
    sender.py             NapCat HTTP send API
    classifier.py         message segment classification
  ingress.py              loopback HTTP service-event ingress
  memory/
    window.py             rolling in-memory context window
    session.py            per-session activity/pending state
    store.py              SQLite long-term store, events, actors, summaries, self notes, media
    actors.py             global actor/conversation source formatting
    projection.py         Life Stream and native tool-round projection
  tools/
    registry.py           Tool and ToolRegistry
    config_manager.py     runtime config get/set/list/reload
    memory.py             memory search/save tools
    self_note.py          self-note management
    send.py               message sending tool
    markdown_renderer.py  Python bridge to Node renderer
  tui/
    tester.py             interactive test runner
    dashboard.py          full-screen runtime dashboard
```

Optional renderer:

```text
tools/markdown-renderer/
  package.json
  render.mjs
  template.css
```

## 6. 配置结构

`config.example.yaml` 是当前配置模板。`config.yaml` 被 gitignored。

关键结构：

```yaml
napcat:
  ws_url: ws://localhost:3000
  http_url: http://localhost:3000
  access_token: ""

model:
  provider: deepseek
  model: deepseek-chat
  api_key: ""
  base_url: https://api.deepseek.com/v1
  temperature: 0.5
  reasoning_effort: max

context:
  model_context_tokens: 131072
  compression_trigger_ratio: 0.8
  compression_target_ratio: 0.5
  reserved_output_tokens: 8192
  recent_actions_max_count: 12
  summaries_max_count: 180
  summaries_min_count: 90
  debounce_timeout: 1.5

prompts:
  system_file: system-prompts.yaml

identity:
  bot_user_id: ""
  bot_nickname: ""
  owner_user_id: ""
  owner_nickname: ""
  owner_alias: ""

heartbeat:
  enabled: true
  private_interval_seconds: 900
  group_interval_seconds: 10800
  active_window_seconds: 86400

ingress:
  enabled: false
  host: 127.0.0.1
  port: 8765
  token: ""
  target_user_id: ""

memory:
  archive_threshold_tokens: 100
  self_note_target_tokens: 1000
  self_note_max_multiplier: 2.0

render:
  markdown_image:
    enabled: false
```

## 7. 运行方式

```powershell
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.main
```

Tester:

```powershell
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.tester
```

Dashboard:

```powershell
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.dashboard config.yaml
```

## 8. 测试与质量门槛

合并到主线前至少运行：

```powershell
$env:PYTHONPATH = "."
python -m pytest tests/ -q
```

涉及 Markdown renderer 时额外运行：

```powershell
cd tools/markdown-renderer
npm run check
```

如果本机已安装 Playwright Chromium，应做一次端到端验证：

```text
send_tool(markdown_image)
-> render_markdown_image()
-> node tools/markdown-renderer/render.mjs
-> PNG file
-> image segment passed to sender
```

## 9. 当前上下文事实源

当前上下文、摘要覆盖、动作账本、图片与提示注入协议统一记录在 `docs/current-design.md`。`bottle/docs/` 保留为历史设计来源，不再作为当前运行时契约。

后续方向包括 Markdown 超长内容分页、长期记忆质量评估、CI，以及在重新确定消息分帧协议后恢复多条 QQ 回复能力。

2026-09-08 在设计阶段暂定[自主编排、独立审查与及时表达意图](docs/agent-orchestration-options-2026-09-08.md)：模型通过输出参数或重定向工具选择目标，消息输出、bash 调用、子代理启动等实际动作经过独立审查；改写请求并交给关闭思考的模型，及时表达有依据、符合角色的意图。固定等待短句和固定回传会话不再作为后续方向，已有原型保留为实验记录；该暂定设计尚未实现或验证。

意图衔接以实际发送的原话和结果为准。执行者及相关子代理在下一次实际行动前读到新增的相关表态，再决定承接或修正；已说出的打算不等于动作已完成，不增加用户授权，也不凌驾于用户的新要求。
