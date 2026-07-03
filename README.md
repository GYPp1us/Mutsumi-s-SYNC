# Mutsumi's SYNC v3

Mutsumi's SYNC v3 is an asynchronous QQ chatbot built on NapCat. It provides an OpenAI-compatible LLM pipeline, cancellable per-session scheduling, tool calling, long-term memory, and TUI tooling for local debugging.

The project was rewritten from the legacy v2 codebase. The current v3 line focuses on a maintainable core: one async pipeline function, scheduler-owned state, explicit dependency injection, and observable logs.

## Features

- NapCat WebSocket message receiving and HTTP sending.
- Per-user/per-group cancellable pipeline tasks.
- OpenAI-compatible LLM provider with DeepSeek reasoning support.
- Built-in tool registry with hot snapshot/version tracking.
- SQLite message store, summaries, self notes, and media storage.
- Context assembly with non-truncated CONTEXT logs.
- Interactive tester with `/inject` and `/break`.
- Dashboard TUI with selectable colored logs, scrolling, copy support, command history, config commands, and memory view.
- Assistant `content` is the normal user-visible reply channel, with `|` splitting for multiple QQ messages.
- `no_reply` tool for deliberate silent turns.
- `send` tool support for special message segments, legacy text sends, images, face, mentions, replies, forwards, and optional Markdown-rendered images.
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
bottle/docs/               # original v3 architecture design references
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
