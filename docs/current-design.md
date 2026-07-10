# Mutsumi's SYNC Current Design

This document is the current design baseline for v3. `bottle/docs/` records the
design history; when it conflicts with this document, the implementation tests,
or `README.md`, this document and the tests take precedence.

## 1. Runtime Boundaries

- `PipelineScheduler` owns shared dependencies and per-conversation state.
- `pipeline()` remains one asynchronous function and receives all state through
  `PipelineDeps`.
- A newer input for the same conversation cancels the previous task with
  `asyncio.Task.cancel()` and waits for its cleanup.
- Incoming user data is persisted before cancellation-sensitive LLM or tool work.
- Heartbeats are real, silent LLM calls with `remember_input=False`; they never
  create conversation, summary, memory, or action records.

## 2. Provider Request Layout

Every LLM request has these layers, in order:

1. A provider-native `system` message containing only stable platform rules.
2. A first `user` message named `[Context Packet]` containing self-note,
   summaries, recent verified actions, and the configured persona prompt at the
   very end. The packet is context, not a fresh user request.
3. The working conversation window as ordinary timestamped `user` and
   `assistant` messages.
4. A temporary `[Runtime Injection]` user message containing current UTC+8 time,
   source, silent/remembering flags, peer metadata, and Priority Override.
5. The current user input.

Runtime Injection is not persisted. Priority Override appears exactly once per
request. Platform timestamps are supplied values, not text the model should
invent. The separate `persona_prompt` configuration value belongs at the end of
the first Context Packet so it shapes interpretation without competing with the
stable tool and safety protocol in `system`.

DeepSeek `reasoning_content` is retained on the assistant message only during
the current native tool loop. It is never sent to QQ and never persisted into a
future conversation window.

## 3. Reply And Tool Protocol

- The final assistant `content` with no `tool_calls` is the ordinary visible
  reply channel.
- Pipe-based multi-message splitting is disabled. `|` is sent literally until a
  replacement framing protocol is designed.
- `send` is reserved for special QQ segments and Markdown-rendered images.
- `no_reply` intentionally ends a turn without visible output.
- Provider tool schemas are authoritative. Prompts must not contain a manually
  maintained tool inventory.
- A tool action is successful only when its returned result proves success.
  Assistant prose is never treated as evidence that a side effect happened.
- Three consecutive actual error results from the same tool stop further calls
  to that tool in the current loop. Successful results reset the counter.

## 4. Persistence And Cancellation

Each inbound conversation record stores its source, lifecycle status, original
input, final visible text when present, and structured image metadata when
present. Valid lifecycle states include `received`, `responded`, `no_reply`,
`empty`, `cancelled`, and `error`.

Memory write tools (`memory_save`, `self_note`, and `priority_override`) are
staged during the tool loop. Their immediate result explicitly says `staged`,
not persisted. Cleanup flushes each staged operation once under cancellation
protection and writes a verified action result. This preserves turn-level
atomicity when a pipeline is interrupted.

## 5. Working Window And Summaries

Raw short messages remain the source of truth. There are two summary purposes:

- `message` summaries describe one long message and do not cover other message
  records.
- `compaction` summaries replace a precise prefix of complete persisted turns
  and carry `covered_through_message_id`.

Legacy `last_message_id` boundaries are untrusted and are not used to skip raw
records. Compaction operates on complete record IDs, never half of a user/bot
turn. MessageWindow entries carry their originating record ID.

Startup restores the newest eligible conversation rows in chronological order.
It uses the same eligibility rules as live window insertion and excludes
memory, self-note, Priority Override, action artifacts, cancelled/error turns,
empty/no-reply turns, and malformed records.

## 6. Token-Aware Compaction

Compaction considers the complete provider request: system rules, Context
Packet, working window, Runtime Injection, current input, and tool schemas.
Before a call it uses a deterministic estimate; after a call it records the
provider's actual `prompt_tokens` when available.

Configuration defines model context capacity, trigger ratio, target ratio, and
reserved output tokens. Compaction removes the oldest complete turns until the
estimated request fits the target. Summary input is not silently truncated; if
it cannot fit the summarizer request, it is split into explicit chunks.
Current native tool-loop messages are never compacted mid-loop.

## 7. Image Input

The classifier preserves both image metadata and all accompanying text. A
configured vision provider produces a textual description. The pipeline then
constructs one synthetic user input containing the caption, description, and a
stable artifact reference, and proceeds through the normal LLM/tool/reply path.
Vision failure is represented in that input rather than replaced by a canned
bot response. The original image metadata and description are persisted
structurally.

## 8. Verified Action Ledger

Tool and send side effects are stored in dedicated structured action records.
Each record includes tool name, call ID, timestamp, success, sanitized arguments,
and result. Successful sent images additionally record message ID, source
Markdown hash/reference, and generated file reference.

A bounded recent-action section may be injected into the Context Packet. Action
records are never inserted as ordinary assistant prose. In particular,
`[sent image: ...]` markers are forbidden in the working conversation window.

## 9. Send Truthfulness

NapCat success requires both an HTTP-success response and `status == "ok"`.
`failed`, malformed, timeout, and transport-error responses become `[Error: ...]`
tool results. Artifacts and successful actions are persisted only after verified
NapCat success. Normal assistant-content sends follow the same check and failed
sends do not become assistant history.

## 10. Configuration Editing

`config_manager set` updates an arbitrary-depth YAML scalar without serializing
or reordering unrelated configuration. It preserves comments and surrounding
formatting. Boolean parsing accepts explicit true/false forms and rejects
unknown strings instead of silently converting them to false.

## 11. Documentation Ownership

- `docs/current-design.md`: current semantic and architectural baseline.
- `README.md`: installation, configuration, operation, and user-facing behavior.
- `AGENTS.md`: agent workflow and invariants.
- `init.md`: project charter and implementation status.
- `bottle/docs/`: historical design source and archived rationale.

## 12. Production Acceptance

A release is complete only after local and server tests pass, the optional
Markdown renderer check passes, the shared production config is patched without
reformatting unrelated values, systemd reports the service active, NapCat is
connected, and fresh logs verify text, tool, image, restart restoration, failed
send, and compaction behavior.

## 13. Delivery Groups

1. Consolidate design and synchronize documentation.
2. Verify NapCat/send result truthfully.
3. Correct summary coverage while preserving raw short messages.
4. Preserve cancellation-safe staged memory writes and verify final outcomes.
5. Restore only clean, runtime-eligible conversation rows.
6. Route captioned image inputs through the regular pipeline.
7. Implement request-level token-aware compaction.
8. Replace assistant artifact markers with a verified action ledger.
9. Disable pipe-based reply splitting.
10. Split `persona_prompt` configuration and inject it at Context Packet tail.
11. Fix arbitrary-depth local YAML editing and strict boolean conversion.
12. Count actual consecutive tool failures and stop at three.
13. Synchronize the canonical system prompt in defaults, docs, and production.
