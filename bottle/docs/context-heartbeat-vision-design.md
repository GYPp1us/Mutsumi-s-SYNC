# Context, Heartbeat, Priority Override, And Vision Design

**Status:** Implemented on v3 mainline  
**Date:** 2026-07-04  
**Scope:** `pipeline.py`, `scheduler.py`, `memory/store.py`, `tools/priority_override.py`, `vision.py`

## 1. Message Layout For LLM Calls

Every LLM request uses exactly one `system` message:

```json
{"role": "system", "content": ""}
```

All platform instructions and durable context are packed into the first `user` message:

- default system prompt
- self-note
- summaries
- future persistent context blocks

That first `user` message is bootstrap context, not a fresh user request. Subsequent user/assistant messages represent only the working conversation window, followed by the current user input.

## 2. Timestamps

Context-facing memory should be time-aware. The context builder formats timestamps in UTC+8 using ISO-like text such as:

```text
2026-07-04T18:20:30+08:00
```

Timestamped inputs:

- summary rows use `summaries.created_at`
- working-window entries use `MessageWindow.created_at`
- self-note and priority override entries store a timestamp when tools add content

Legacy self-note or priority lines without a timestamp are injected with:

```text
[很久之前] original line
```

## 3. Priority Override

`priority_override` is a built-in write tool with:

- `add`
- `replace`
- `clear`

It is stored in `messages` with category `priority_override`, like self-note. The active value is appended after every user-role message in the LLM context, including the bootstrap user message, every user message in the working window, and the current user input.

This is intentionally expensive in context tokens. It should only be used for high-priority instructions that must remain salient.

## 4. Durable Inbound Messages

Text pipelines save an inbound message record before LLM/tool work:

```json
{
  "user": "...",
  "bot": null,
  "status": "received",
  "source": "user"
}
```

The same row is updated at completion:

- `responded`
- `empty`
- `no_reply`
- `cancelled`
- `error`

If cancellation happens while the initial save is returning, the pipeline recovers the recent inbound record by group, category, message text, status, and source, then updates it to `cancelled`.

## 5. Sent Image Artifacts

`send(markdown_image=...)` returns artifact metadata to the pipeline:

```json
{
  "kind": "sent_image",
  "source": "markdown_image",
  "file": "data/generated/markdown/...",
  "markdown": "# source"
}
```

The pipeline stores this as a category `image` message and adds a concise bot-side window entry like:

```text
[sent image: markdown_image, file=..., message_id=...]
```

This lets later context remember that the bot sent an image without OCRing the rendered Markdown image. The Markdown source is the source of truth.

## 6. Heartbeat

The scheduler starts a heartbeat task when `heartbeat.enabled` is true.

Defaults:

```yaml
heartbeat:
  enabled: true
  interval_seconds: 2700
  aggressive_provider_cache_retention: false
```

Heartbeat calls the real pipeline with:

```python
PipelineDeps(
    source="heartbeat",
    silent=True,
    remember_input=False,
)
```

Consequences:

- real LLM call is made
- LLM health is updated
- visible QQ output is suppressed
- heartbeat input is not written to the message store
- working window and summaries are not updated
- write tools are suppressed

When `aggressive_provider_cache_retention` is true, heartbeat prefers the last active conversation key or an existing window key. Otherwise it uses a dedicated `private:heartbeat` key.

## 7. Vision Provider

Image recognition uses a separate provider. The main text model is not assumed to support images.

OpenAI-compatible chat/completions provider:

```yaml
vision:
  enabled: false
  provider: openai-compatible
  model: ""
  api_key: ""
  base_url: ""
  timeout_seconds: 60
```

This provider accepts either `image_url` or a local image file converted to a data URL. Its prompt asks for concise memory-oriented descriptions and preservation of visible text, formulas, code, and diagrams.

Volcengine OCR provider:

```yaml
vision:
  enabled: false
  provider: volcengine-ocr
  access_key_id: ""
  secret_access_key: ""
  session_token: ""
  region: cn-north-1
  service: cv
  action: OCRNormal
  version: "2020-08-26"
```

This provider calls Volcengine Visual OCR `OCRNormal` at `https://visual.volcengineapi.com` using HMAC-SHA256 request signing. It accepts either `image_url` or a local image file encoded as raw base64 and returns visible OCR text as the image description. It requires an Access Key ID and Secret Access Key pair; `session_token` is included and signed when temporary credentials are used. A single bearer-style API key or session token is not sufficient for this OCR API.

Incoming image messages save structured JSON with:

- source message
- bot reply
- `image_file`
- `image_url`
- `image_description`

## 8. Tests

Relevant tests:

- `tests/test_pipeline_e2e.py`
- `tests/test_scheduler_smoke.py`
- `tests/test_store_memory.py`
- `tests/test_vision.py`
- `tests/test_config.py`
