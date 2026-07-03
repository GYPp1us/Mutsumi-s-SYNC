# Mutsumi's SYNC v3 - 项目章程

**状态**: v3 主线可用，继续迭代中  
**主线分支**: `main`  
**v3 集成分支**: `feature/v3-rewrite`  
**旧版存档**: `archive/legacy` tag

---

## 1. 项目定位

Mutsumi's SYNC v3 是一个基于 NapCat 的 QQ 聊天机器人。它不是简单的回复脚本，而是一个可以接入 OpenAI-compatible LLM、调用工具、维护长期记忆、观察 pipeline 状态、并由 TUI 辅助调试的个人 Agent 运行时。

核心设计取向：

- 运行时简单明确，避免 LangChain 式重抽象。
- Pipeline 维持单函数，便于 AI agent 和人类维护者一次性理解完整消息处理流程。
- Scheduler 持有状态，Pipeline 只消费依赖。
- 工具系统可热更新，并能在同一次 pipeline 调用中感知变化。
- 本地测试和真实 NapCat 运行路径尽量一致。

## 2. 技术栈

| 组件 | 选型 |
| --- | --- |
| Runtime | Python 3.11+, asyncio |
| QQ I/O | NapCat WebSocket + HTTP API |
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
| Dashboard TUI | 完成第一版 |
| 交互式 tester | 完成 |
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
  memory/
    window.py             rolling in-memory context window
    session.py            per-session activity/pending state
    store.py              SQLite long-term store, summaries, self notes, media
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
  window_max_tokens: 100000
  window_min_tokens: 50000
  summaries_max_count: 180
  summaries_min_count: 90
  debounce_timeout: 1.5

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

## 9. 后续方向

- 将 Dashboard 与 tester 的注册表构造进一步去重。
- 为 Markdown 图片渲染补分页策略，避免超长内容单图过高。
- 继续完善长期记忆策略与摘要质量。
- 增加 CI，覆盖 Python tests 与 Node renderer check。
- 梳理 `bottle/docs/`，把原始设计文档升级为当前架构文档。

