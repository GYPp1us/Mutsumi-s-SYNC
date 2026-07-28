# Mutsumi's SYNC v3

Mutsumi's SYNC v3 is an asynchronous QQ chatbot built on NapCat. It provides an OpenAI-compatible LLM pipeline, cancellable per-session scheduling, tool calling, long-term memory, and TUI tooling for local debugging.

The project was rewritten from the legacy v2 codebase. The current v3 line focuses on a maintainable core: one async pipeline function, scheduler-owned state, explicit dependency injection, and observable logs.

## Features

- NapCat WebSocket message receiving and HTTP sending.
- Per-user/per-group cancellable pipeline tasks.
- OpenAI-compatible LLM provider with DeepSeek reasoning support.
- Built-in tool registry with hot snapshot/version tracking.
- SQLite message store, summaries, self notes, and media storage.
- Global Event Ledger with provenance-preserving cross-conversation projections and idle Episode summaries.
- Pipeline-native Media Ledger with SHA deduplication, stable media IDs, and global sticker search/maintenance tools.
- Explicit `bot_state` canonical projection for facts about the bot itself, separate from private relationship memory.
- Layered context assembly: stable provider-native Chinese `system`, global `Self Context`, conversation-scoped `Conversation Context`, working window, temporary `Runtime Injection`, and current input.
- A separate persona prompt is loaded from `system-prompts.yaml` and injected into `Self Context`; it is never edited through `config.yaml`.
- Global inner journal for subjective bot-state continuity, with source conversation/actor provenance and bounded token/entry retention.
- Request-level token budgeting over messages and tool schemas, with exact complete-turn compaction boundaries.
- Append-only NDJSON stream logs for durable real-time diagnostics.
- Rotating human-readable text logs for `tail -f` and `grep`.
- Priority Override memory, injected once per request in `Runtime Injection` for unusually important instructions.
- Proactive heartbeat checks every 15 minutes for private chats and every 3 hours for groups active within the last 24 hours.
- Optional vision providers for image-to-text descriptions, including OpenAI-compatible chat/completions and Volcengine OCR.
- Durable inbound message persistence before LLM calls, so cancelled pipelines do not silently drop user input.
- Interactive tester with `/inject` and `/break`.
- Structured action ledger for verified tool/send outcomes; generated-image markers never enter assistant history.
- Dashboard TUI and tester as local debugging surfaces; production behavior is defined by `main.py` and may have a different registry.
- Final assistant output uses a strict `[TO_SELF]...[/TO_SELF]` plus `[TO_USER]...[/TO_USER]` envelope. Only `TO_USER` is visible; `TO_SELF` goes to the global inner journal.
- If a model omits all envelope markers, the pipeline recovers its plain final content as `TO_USER` with an empty `TO_SELF`; malformed marker-bearing output still receives a bounded protocol rewrite.
- Assistant `TO_USER` is sent as one QQ message; `|` is literal text.
- `status_update` can send one short progress message before a long tool; automatic fallback progress never enters assistant history.
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

- Python 3.10+
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
  model_context_tokens: 100000
  compression_trigger_ratio: 0.8
  compression_target_ratio: 0.5
  reserved_output_tokens: 8192
  recent_actions_max_count: 12
  summaries_max_count: 180
  summaries_min_count: 90
  episode_idle_seconds: 1800
  episode_max_events: 160

prompts:
  system_file: system-prompts.yaml

inner_journal:
  max_entry_chars: 1000
  max_context_tokens: 16000
  max_context_entries: 200

heartbeat:
  enabled: true
  private_interval_seconds: 900
  group_interval_seconds: 10800
  active_window_seconds: 86400

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

Dashboard commands include:

- Real-time colored logs.
- Log selection and Ctrl+C copy.
- PageUp/PageDown scrolling independent from command cursor focus.
- Command history with Up/Down.
- `/watch`, `/auto`, `/memory`, `/config`, `/inject`, `/break`, `/connect`.

The Dashboard and tester are diagnostic surfaces, not production registry authorities. Verify production tool behavior through `src/mutsumi_sync/main.py`, tests, and service logs.

## Streaming Logs

Production logging still writes to stdout for systemd/journald, and TUI tools still consume in-memory queues for live display. In addition, file logging can write every `mutsumi.*` log record to two rotating files:

- `logging.stream_store` writes append-only NDJSON to `data/logs/mutsumi.ndjson` for machine parsing, replay, and future UI/indexing.
- `logging.text_file` writes ordinary human-readable text to `data/logs/mutsumi.log` for `tail -f`, `grep`, and quick server diagnosis.

Each line is one JSON object with schema `mutsumi.log.v1`, timestamp, level, logger name, raw message, source location, process, and thread metadata. Multi-line records such as `CONTEXT`, LLM results, and tool logs remain one JSON record instead of being split into separate storage events. `keep_ansi` preserves colored diagnostic blocks for replay; set it to `false` to store plain text.

The text log uses readable UTC+8 timestamps and strips ANSI color by default. It is deliberately redundant with the NDJSON store: the text file is for humans, while NDJSON remains the durable structured source.

## Reset Production Data

The production host has `/opt/mutsumi-sync-v3/reset_production_data.sh` for clearing test or historical bot state. It stops the backend, removes everything under `/opt/mutsumi-sync-v3/shared/data` (SQLite history, logs, media artifacts, and temporary files), and starts the backend again. Configuration, code, and shared system prompts are preserved.

Run a preview first, then confirm interactively:

```bash
/opt/mutsumi-sync-v3/reset_production_data.sh --dry-run
/opt/mutsumi-sync-v3/reset_production_data.sh
```

Use `--yes` only when the data reset has been explicitly confirmed.

## LLM Output Protocol

Only the final LLM round without `tool_calls` is parsed. It must contain exactly:

```text
[TO_SELF]
简短的主观状态、意图或未完成事项；不是原始 CoT。
[/TO_SELF]
[TO_USER]
发给用户的扁平纯文本。
[/TO_USER]
```

`TO_USER` is the only ordinary visible channel. It is subject to the flat-text
gate; complex Markdown, LaTeX, code, or Mermaid must use
`send(markdown_image=...)`. `TO_SELF` is stored globally only after a complete,
successful, non-cancelled pipeline turn. Protocol tags are not written into the
conversation window or durable assistant reply.

Pipe-based multi-message framing is disabled while a replacement protocol is being designed. `a | b` and `a \| b` are both sent unchanged in one QQ message.

Reasoning content is retained only in the current DeepSeek tool loop and is never sent or persisted. Tool-loop assistant content is never sent. Tools are for memory, config, queries, external APIs, special message segments, or silent control. For ordinary text replies, use `TO_USER`; do not call `send`.

Use `no_reply` when the turn should intentionally produce no visible message. The `send` tool remains available for special segments such as `markdown_image`, image, face, mention, reply, and forward.

Use `status_update` before a tool that is expected to take a noticeable amount of time. It sends a short user-visible progress message, does not end the pipeline, and is recorded as an event/action rather than an assistant history turn. The pipeline sends one generic fallback when a registered long tool is called without an explicit status update. Heartbeat pipelines suppress progress messages but may send a verified proactive final message.

## Context And Memory Protocol

The LLM request uses one provider-native Chinese `system` message for durable platform rules. The next user message is `[Self Context]`, containing persona, canonical bot state, and the global inner journal. The following user message is `[Conversation Context]`, containing conversation-scoped self notes, summaries, and projected life context. These are documentary context, not fresh user requests. The verified action ledger remains durable audit data but is not injected into every request. Later user/assistant messages are the working conversation window.

Inner journal entries are subjective bot-state deltas, not verified facts or instructions. They include source conversation/actor provenance for the model, but are never represented as fake user/assistant turns. A successful final turn may append one bounded `TO_SELF` entry; cancellation, failed visible output, and malformed output do not.

Summaries, self notes, and historical user turns are annotated with readable UTC+8 timestamps. Historical assistant turns are passed through without synthetic timestamp prefixes. Older self-note lines without timestamps are injected as `很久之前`.

Before the current user request, the pipeline injects a temporary `[Runtime Injection]` user message with current UTC+8 time, source, silent/remembering flags, peer metadata, and the active Priority Override. Runtime Injection is platform state, not user-authored chat, and is not written to durable history.

`priority_override` is a write tool with `add`, `replace`, and `clear`. Its active content is injected only in Runtime Injection. Use it only for high-priority rules that are worth paying attention to every turn.

Inbound user text is saved before the LLM call. If the task is cancelled, the saved record is updated to `status=cancelled` instead of being lost. Heartbeat pipelines set `remember_input=false` and `remember_output=true`: synthetic heartbeat input is not written, while verified proactive assistant output is written to the target window and event ledger.

Per-message summaries describe only one long message and never claim database coverage. Request compaction summarizes a precise prefix of complete record-ID turns and stores a trusted `covered_through_message_id`. Legacy `last_message_id` values are ignored during restart restoration. Only successful conversation records are restored; memory, action artifacts, cancelled/error/no-reply records are excluded.

Memory write tools are staged during the tool loop and committed exactly once during cancellation-protected cleanup. Their immediate tool result says `staged`, while the final success or failure is stored in the action ledger.

### Global Event And Media Ledger

The `events` table is the append-first interaction ledger. It records actor,
conversation, visibility, lifecycle, tool, and media provenance. Global storage
does not mean global prompt injection: private events remain private, group
events remain in their group, and only explicitly global records cross
conversations. Cross-conversation records are documentary data with actor IDs,
never simulated `user` or `assistant` turns.

A group has one shared conversation window while members retain separate actor
and legacy memory scopes. After about 30 minutes of idle time, finalized events
may be summarized into an Episode with exact sequence coverage. Raw events are
never deleted; context projection chooses either the Episode or its covered raw
events, keeping requests near the 100K-token attention budget.

Incoming and successfully outgoing media are automatically registered with a
stable SHA-derived `media_id`; inbound image URLs are downloaded into the
ledger when possible and otherwise retained as external references. Image
context contains only the description and `media_id`, not CQ metadata or
temporary URLs. `sticker_search` without a query lists all available stickers;
`sticker_manage` updates descriptions or lifecycle status.

System prompt ownership is centralized in `system-prompts.yaml`. The runtime,
summary workers, and context experiment load the `persona` field plus the same
four operational prompt levels through `prompts.system_file`, so historical data is interpreted consistently at every
LLM boundary. `config_manager reload` reloads both files. Production keeps the
prompt file at `/opt/mutsumi-sync-v3/shared/system-prompts.yaml` so releases do
not overwrite operator changes.

## Heartbeat And Vision

The scheduler runs a real proactive heartbeat scan every 15 minutes for private conversations and every 3 hours for groups. Only conversations with a real inbound event in the previous 24 hours are checked. Heartbeats yield to user pipelines, suppress cold-session pokes and progress notifications, and do not expose configuration, memory-write, or scheduler tools.

Incoming image messages can use a separate vision provider when `vision.enabled` is true. `provider: openai-compatible` uses `vision.model`, `vision.base_url`, and `vision.api_key`. `provider: volcengine-ocr` uses Volcengine Visual OCR `OCRNormal` with `vision.access_key_id` and `vision.secret_access_key`; `vision.session_token` is optional for temporary credentials. Captions and image metadata are preserved, the vision description or error is assembled into a synthetic user input, and that input runs through the normal LLM/tool/reply pipeline.

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

The canonical semantic design is [docs/current-design.md](docs/current-design.md). Files under `bottle/docs/` are historical design sources rather than current behavior contracts.

Heartbeat pipelines use `remember_input=False` and `remember_output=True`. Ordinary user pipelines keep `remember_input=True` and persist the inbound message before any cancellation-sensitive LLM or tool work.

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
