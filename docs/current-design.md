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
- Heartbeats are real LLM calls over conversations with inbound activity in the
  previous 24 hours. Private conversations are scanned every 15 minutes and
  groups every 3 hours. Synthetic heartbeat input uses `remember_input=False`,
  while verified proactive assistant output may be persisted to the target
  conversation and global event ledger.

## 2. Provider Request Layout

The persona prompt and four operational prompts live in the standalone
`system-prompts.yaml` file and are selected through `prompts.system_file`.
Runtime, message-summary, summary-merge, and Episode-summary requests load that
same validated file; missing or empty operational levels fail startup.
`config_manager reload`
reloads the external prompt file. Production stores it under the shared deploy
root so a release does not overwrite operator changes.

Every LLM request has these layers, in order:

1. A provider-native Chinese `system` message containing stable platform rules and the configured persona.
2. The global chronological Life Stream. Human and service inputs use provider `user`, assistant outputs use `assistant`, and completed tool rounds use native `assistant(tool_calls)` plus `tool` messages. Actor and conversation identity is carried by readable source prefixes.
3. A temporary platform-state `user` message containing current UTC+8 time, source, actor, peer metadata, flags and active work.
4. The current source message.

Platform state is not persisted. Platform timestamps are supplied values, not
text the model should invent. The external `persona` prompt is loaded from
`system-prompts.yaml`, appended to stable `system`, and is not stored in
`config.yaml`.

DeepSeek `reasoning_content` is retained on the assistant message only during
the current native tool loop. It is never sent to QQ and never persisted into a
future conversation window.

## 3. Reply And Tool Protocol

- The final assistant `content` with no `tool_calls` must contain exactly two
  blocks: `[TO_SELF]...[/TO_SELF]` followed by `[TO_USER]...[/TO_USER]`.
- For model compatibility, final content with no protocol markers is recovered
  as empty `TO_SELF` plus `TO_USER`; marker-bearing malformed replies still use
  the bounded rewrite loop. Strict parsing remains available for validation.
- `TO_USER` is the only ordinary visible channel. It is flat-text gated and is
  sent once. `TO_SELF` is a bounded subjective delta stored in global inner
  journal only after a complete successful turn; protocol tags never enter the
  working window or durable assistant reply.
- Assistant content from a tool-call round is never sent or parsed. DeepSeek
  `reasoning_content` is retained only for the current provider-native loop.
- Pipe-based multi-message splitting is disabled. `|` is sent literally until a
  replacement framing protocol is designed.
- `send` is reserved for special QQ segments and Markdown-rendered images.
- `status_update` is a short visible progress event for long tools. It executes
  before other calls in its round, does not end the pipeline, and is not added to
  assistant history. A long registered tool without an explicit update gets
  one pipeline-generated fallback; heartbeat pipelines suppress progress events.
- `no_reply` intentionally ends a turn without visible output.
- Provider tool schemas are authoritative. Prompts must not contain a manually
  maintained tool inventory.
- A tool action is successful only when its returned result proves success.
  Assistant prose is never treated as evidence that a side effect happened.
- Three consecutive actual error results from the same tool stop further calls
  to that tool in the current loop. Successful results reset the counter.

## 4. Persistence And Cancellation

The global `inner_journal` table stores only bounded subjective bot-state
deltas from `TO_SELF`. Each entry keeps its source conversation, source actor,
pipeline id, and source event ids. It is injected as documentary Life Stream
state with a clear warning that it is subjective history, not verified facts or
instructions.
Entries are latest-content deduplicated and bounded by configured entry count,
character count, and context token budget. A final turn that sends no visible
text may still commit `TO_SELF` when it completes through `no_reply` or a
successful special send.

Each inbound conversation record stores its source, lifecycle status, original
input, final visible text when present, and structured image metadata when
present. Valid lifecycle states include `received`, `responded`, `no_reply`,
`empty`, `cancelled`, and `error`.

Memory write tools (`memory_save` and `self_note`) are
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
memory, self-note, action artifacts, cancelled/error turns, empty/no-reply
turns, and malformed records.

If startup restoration is capped and older eligible rows remain outside the
window, that window is marked coverage-untrusted. It may compact in memory but
must not advance `covered_through_message_id`; repeated restoration is preferable
to claiming history that was never summarized.

## 6. Token-Aware Compaction

Compaction considers the complete provider request: system rules, Life Stream,
platform state, current input, and tool schemas.
Before a call it uses a deterministic estimate; after a call it records the
provider's actual `prompt_tokens` when available.

Configuration defines model context capacity, trigger ratio, target ratio, and
reserved output tokens. Compaction removes the oldest complete turns until the
estimated request fits the target. Summary input is not silently truncated; if
it cannot fit the summarizer request, it is split into explicit chunks.
Current native tool-loop messages are never compacted mid-loop.

## 7. Image Input

The classifier preserves both image metadata and all accompanying text. A
configured vision provider produces a textual description. The pipeline first
downloads an image URL into the Media Ledger when possible, falling back to an
external reference if the URL cannot be fetched. It then constructs one
synthetic user input containing only the caption, description, and stable media
reference; CQ metadata, temporary URLs, and local source paths are not placed
in model context. The pipeline proceeds through the normal LLM/tool/reply path.
Vision failure is represented in that input rather than replaced by a canned
bot response. The original image metadata and description are persisted
structurally.

## 8. Verified Action Ledger

Tool and send side effects are stored in dedicated structured action records.
Each record includes tool name, call ID, timestamp, success, sanitized arguments,
and result. Successful sent images additionally record message ID, source
Markdown hash/reference, and generated file reference.

Action records are audit and recovery data, not default prompt material. Current
tool-loop results still use provider-native tool messages; historical actions
should be retrieved only when a task actually needs them. Action records are
never inserted as ordinary assistant prose. In particular, `[sent image: ...]`
markers are forbidden in the working conversation window.

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

## 11. Global Event Ledger And Conversation Boundaries

`events` is the append-first global Life Stream. It records inbound/outbound
messages, tool calls/results, media, and state changes with a monotonic global
sequence plus `conversation_id`, `actor_id`, `actor_kind`, `visibility`,
`audience`, `turn_id`, and pipeline id. Heartbeat input and Runtime Injection
are platform state and are excluded from the lived interaction stream;
successful proactive assistant output is a real outbound event.

The current projector intentionally presents finalized events from the unified
timeline. All human and service sources use provider `user`; readable actor and
conversation prefixes preserve identity. `actors` is the global registry for
stable display names, private aliases and relationship labels. Visibility and
audience remain attached to every event for future input/output gates and audit.

Group runtime state uses `group:<group_id>` as the conversation and retains the
legacy `group:<group_id>:<actor_id>` key only for actor-scoped memory/action
compatibility. Each human remains an independent actor inside the shared group.
The current actor comes from platform metadata and must not be inferred from
historical text.

`episodes` are derived summaries, never replacements for ledger facts. An
episode belongs to one conversation and stores exact first/last event
sequences, participants, narrative, open loops, and media references. A
summary is requested after about 30 minutes of idle time or when the context
budget needs it; only finalized events are eligible. Failed summarization leaves
raw events usable. Projection selects an episode or its covered raw events,
never both. The target request is approximately 100K tokens.

Media is pipeline-native infrastructure. Incoming and successfully outgoing
media receive stable SHA-derived `media_id` values and are recorded with
descriptions, references, and lifecycle status. `sticker_search` and
`sticker_manage` query or maintain this ledger; they are not required for the
pipeline to remember that media happened.

`bot_state` and `priority_override` are retired model capabilities. Their legacy
tables may remain until a production data reset, but they are not registered or
injected. Current bot-self continuity is carried by the inner journal and the
unified event timeline.

## 12. Output Gate

Only `TO_USER` is subject to the ordinary flat-text gate. Obvious complex
Markdown (headings, tables, code fences, links/images, or LaTeX) is rejected
before sending and a bounded correction is returned to the current model loop.
The model must rewrite it as plain text or use `send.markdown_image`. A rejected
response is never persisted as a sent outbound event, and its `TO_SELF` is not
eligible for inner-journal commit.

## 13. Documentation Ownership

- `docs/current-design.md`: current semantic and architectural baseline.
- `README.md`: installation, configuration, operation, and user-facing behavior.
- `AGENTS.md`: agent workflow and invariants.
- `init.md`: project charter and implementation status.
- `bottle/docs/`: historical design source and archived rationale.

## 14. Production Acceptance

A release is complete only after local and server tests pass, the optional
Markdown renderer check passes, the shared production config is patched without
reformatting unrelated values, systemd reports the service active, NapCat is
connected, and fresh logs verify text, tool, image, restart restoration, failed
send, and compaction behavior.

## 15. Delivery Groups

1. Consolidate design and synchronize documentation.
2. Verify NapCat/send result truthfully.
3. Correct summary coverage while preserving raw short messages.
4. Preserve cancellation-safe staged memory writes and verify final outcomes.
5. Restore only clean, runtime-eligible conversation rows.
6. Route captioned image inputs through the regular pipeline.
7. Implement request-level token-aware compaction.
8. Replace assistant artifact markers with a verified action ledger.
9. Disable pipe-based reply splitting.
10. Keep the external `persona` prompt separate from `config.yaml` and append it
    to the stable provider-native `system` message.
11. Fix arbitrary-depth local YAML editing and strict boolean conversion.
12. Count actual consecutive tool failures and stop at three.
13. Synchronize the canonical system prompt in defaults, docs, and production.
