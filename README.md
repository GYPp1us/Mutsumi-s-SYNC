# Mutsumi's SYNC v3

Mutsumi's SYNC v3 is an asynchronous QQ chatbot built on NapCat. It provides an OpenAI-compatible LLM pipeline, cancellable per-session scheduling, tool calling, long-term memory, and TUI tooling for local debugging.

The project was rewritten from the legacy v2 codebase. The current v3 line focuses on a maintainable core: one async pipeline function, scheduler-owned state, explicit dependency injection, and observable logs.

## Features

- NapCat WebSocket message receiving and HTTP sending.
- Per-user/per-group cancellable pipeline tasks.
- OpenAI-compatible LLM provider with DeepSeek reasoning support.
- Built-in tool registry with hot snapshot/version tracking.
- SQLite message store, summaries, self notes, and media storage.
- Context assembly with provider-native `system`, a persistent `Context Packet`, and a temporary `Runtime Injection`.
- Append-only NDJSON stream logs for durable real-time diagnostics.
- Rotating human-readable text logs for `tail -f` and `grep`.
- Priority Override memory, injected once per request in `Runtime Injection` for unusually important instructions.
- Silent heartbeat pipeline every 45 minutes, using a real LLM call without remembering heartbeat inputs.
- Optional vision providers for image-to-text descriptions, including OpenAI-compatible chat/completions and Volcengine OCR.
- Durable inbound message persistence before LLM calls, so cancelled pipelines do not silently drop user input.
- Interactive tester with `/inject` and `/break`.
- Dashboard TUI with selectable colored logs, scrolling, copy support, command history, config commands, and memory view.
- Assistant `content` is the normal user-visible reply channel, with `|` splitting for multiple QQ messages.
- `no_reply` tool for deliberate silent turns.
- `send` tool support for special message segments, legacy text sends, images, face, mentions, replies, forwards, and optional Markdown-rendered images.
- `scheduler` tool for durable one-shot scheduled pipeline triggers.
- Optional Node/Playwright Markdown renderer for LaTeX, highlighted code blocks, and Mermaid diagrams.

## Repository Layout

```text
src/mutsumi_sync/
  main.py                  # production entry and tool registration
  scheduler.py             # PipelineScheduler, task lifecycle, shared state
  pipeline.py              # single async message-processing function
  config.py                # Pydantic config and YAML persistence
  logging.py               # logging helpers
  message/                 # NapCat receiver/sender/classifier
  memory/                  # window/session/store
  tools/                   # built-in tools
  tui/
    tester.py              # interactive test runner
    dashboard.py           # full-screen dashboard
tools/markdown-renderer/   # optional Node renderer for Markdown images
scripts/                   # optional install scripts
tests/                     # pytest suite
bottle/docs/               # architecture references, including current context/heartbeat/vision design
```

## Requirements

- Python 3.11+
- NapCat for real QQ I/O
- Node.js 20+ only if `send.markdown_image` is enabled

Python dependencies are listed in `requirements.txt`.

## Quick Start

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item config.example.yaml config.yaml
# Edit config.yaml.

$env:PYTHONPATH = "."
python -m src.mutsumi_sync.main
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# Edit config.yaml.

PYTHONPATH=. python -m src.mutsumi_sync.main
```

## Configuration

`config.yaml` is gitignored. Start from `config.example.yaml`.

Important sections:

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

heartbeat:
  enabled: true
  interval_seconds: 2700
  aggressive_provider_cache_retention: false

logging:
  stream_store:
    enabled: true
    path: data/logs/mutsumi.ndjson
    max_bytes: 52428800
    backup_count: 5
    keep_ansi: true
  text_file:
    enabled: true
    path: data/logs/mutsumi.log
    max_bytes: 52428800
    backup_count: 5
    keep_ansi: false

vision:
  enabled: false
  provider: openai-compatible
  model: ""
  api_key: ""
  base_url: ""
  access_key_id: ""
  secret_access_key: ""
  session_token: ""
  region: cn-north-1
  service: cv
  action: OCRNormal
  version: "2020-08-26"

render:
  markdown_image:
    enabled: false
```

If no LLM API key is configured, the pipeline can still run in local stub/testing flows.

## Interactive Tester

The tester is the fastest way to exercise the pipeline without NapCat:

```powershell
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.tester
```

Examples:

```text
/inject private 123 hello
/inject group 456 123 hello from group
/break private 123
/connect
```

`/connect` switches from FakeSender to real NapCat I/O.

## Dashboard TUI

```powershell
$env:PYTHONPATH = "."
python -m src.mutsumi_sync.tui.dashboard config.yaml
```

Dashboard highlights:

- Real-time colored logs.
- Log selection and Ctrl+C copy.
- PageUp/PageDown scrolling independent from command cursor focus.
- Command history with Up/Down.
- `/watch`, `/auto`, `/memory`, `/config`, `/inject`, `/break`, `/connect`.

## Streaming Logs

Production logging still writes to stdout for systemd/journald, and TUI tools still consume in-memory queues for live display. In addition, file logging can write every `mutsumi.*` log record to two rotating files:

- `logging.stream_store` writes append-only NDJSON to `data/logs/mutsumi.ndjson` for machine parsing, replay, and future UI/indexing.
- `logging.text_file` writes ordinary human-readable text to `data/logs/mutsumi.log` for `tail -f`, `grep`, and quick server diagnosis.

Each line is one JSON object with schema `mutsumi.log.v1`, timestamp, level, logger name, raw message, source location, process, and thread metadata. Multi-line records such as `CONTEXT`, LLM results, and tool logs remain one JSON record instead of being split into separate storage events. `keep_ansi` preserves colored diagnostic blocks for replay; set it to `false` to store plain text.

The text log uses readable UTC+8 timestamps and strips ANSI color by default. It is deliberately redundant with the NDJSON store: the text file is for humans, while NDJSON remains the durable structured source.

## LLM Output Protocol

Assistant `content` is user-visible. The pipeline sends only the final LLM round that has no `tool_calls`.

Use an unescaped `|` to split one assistant `content` into multiple QQ messages:

```text
第一条|第二条|第三条
```

Use `\|` when the reply needs a literal pipe character:

```text
a \| b|下一条
```

Reasoning content is logged for debugging but is never sent to users. Tools are for memory, config, queries, external APIs, special message segments, or silent control. For ordinary text replies, write assistant `content`; do not call `send`.

Use `no_reply` when the turn should intentionally produce no visible message. The `send` tool remains available for special segments such as `markdown_image`, image, face, mention, reply, and forward.

## Context And Memory Protocol

The LLM request uses a provider-native `system` message for durable platform rules. The first `user` message is a `[Context Packet]` containing self notes, summaries, and other persistent background context; it is not a fresh user request. Later user/assistant messages are the working conversation window.

Summaries, self notes, and window messages are annotated with readable UTC+8 timestamps. Older self-note lines without timestamps are injected as `很久之前`.

Before the current user request, the pipeline injects a temporary `[Runtime Injection]` user message with current UTC+8 time, source, silent/remembering flags, peer metadata, and the active Priority Override. Runtime Injection is platform state, not user-authored chat, and is not written to durable history.

`priority_override` is a write tool with `add`, `replace`, and `clear`. Its active content is injected only in Runtime Injection. Use it only for high-priority rules that are worth paying attention to every turn.

Inbound user text is saved before the LLM call. If the task is cancelled, the saved record is updated to `status=cancelled` instead of being lost. Heartbeat pipelines set `remember_input=false`, so they do not write message records, update windows, or create summaries.

## Heartbeat And Vision

The scheduler can run a silent heartbeat pipeline every 45 minutes. It performs a real LLM call and reports LLM health, but suppresses visible QQ output, suppresses cold-session pokes, and does not remember the heartbeat input. When `heartbeat.aggressive_provider_cache_retention` is enabled, the heartbeat uses the most relevant active conversation key to keep provider-side prompt caches warm more aggressively.

Incoming image messages can use a separate vision provider when `vision.enabled` is true. `provider: openai-compatible` uses `vision.model`, `vision.base_url`, and `vision.api_key`. `provider: volcengine-ocr` uses Volcengine Visual OCR `OCRNormal` with `vision.access_key_id` and `vision.secret_access_key`; `vision.session_token` is optional for temporary credentials. It extracts visible text and stores it as the image description. The description is saved with the image record and added to the working window.

## Scheduled Tasks

The built-in `scheduler` tool registers durable one-shot tasks. The LLM must provide a formatted `scheduled_time`; `prompt` is optional:

```json
{
  "scheduled_time": "2026-07-08 09:30:00 +08:00",
  "prompt": "提醒用户检查服务器日志"
}
```

Accepted time strings are ISO-like formatted datetimes such as `2026-07-08 09:30:00 +08:00` or `2026-07-08T09:30:00+08:00`. If timezone is omitted, UTC+8 is assumed. The tool returns the task id, normalized trigger time, and a readable duration from now to the trigger time.

Tasks are stored in SQLite and restored on startup. When a task fires, it runs the normal pipeline with `source="schedule"` and message content prefixed as `[SCHEDULED:<id>] ...`, so the model can decide whether to send a visible reply, call tools, update memory, or call `no_reply`.

## Markdown Image Sending

For rich content, the `send` tool can render Markdown source into a PNG and send it as an image segment:

```json
{
  "markdown_image": "# Report\n\n$$E=mc^2$$\n\n```python\nprint('hello')\n```\n\n```mermaid\ngraph TD; A-->B\n```"
}
```

Install the optional renderer:

Windows:

```powershell
.\scripts\install_markdown_renderer.ps1
```

Linux:

```bash
sh scripts/install_markdown_renderer.sh
```

Then enable:

```yaml
render:
  markdown_image:
    enabled: true
```

The renderer uses:

- `markdown-it`
- KaTeX fonts and rendering
- `highlight.js`
- Mermaid
- Playwright Chromium screenshots

The generated PNG files are written to `data/generated/markdown/` by default.

## Tests

```powershell
$env:PYTHONPATH = "."
python -m pytest tests/ -q
```

Optional renderer check:

```powershell
cd tools/markdown-renderer
npm run check
```

## Architecture Notes

The core invariant is that `pipeline()` remains one async function. It receives all dependencies through `PipelineDeps` and should not own global state. Scheduler owns shared config/tool/store/sender state plus per-session windows, sessions, and tasks.

Cancellation is native asyncio cancellation: a newer message cancels the previous task for the same key via `Task.cancel()`.

Tool registry changes are tracked by a monotonic `registry.version`. Pipelines compare their snapshot version after tool calls so same-invocation tool changes are visible on the next LLM round.

Heartbeat pipelines use `PipelineDeps(source="heartbeat", silent=True, remember_input=False)`. Ordinary user pipelines keep `remember_input=True` and persist the inbound message before any cancellation-sensitive LLM or tool work.

## Git Hygiene

Do not commit:

- `config.yaml`
- `.env`
- `data/`
- `tools/markdown-renderer/node_modules/`
- local logs or generated cache files

Use Chinese conventional commit style, for example:

```text
feat: 支持send工具渲染Markdown图片
fix: 完善dashboard日志与上下文管理
docs: 更新v3说明文档
```
