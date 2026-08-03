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
- External service reports are persisted as `received` Event Ledger entries before
  acknowledgement and processed by a per-service FIFO worker; they do not use
  the ordinary same-key cancellation rule.
- Heartbeats are real LLM calls over conversations with inbound activity in the
  previous 24 hours. Private conversations are scanned every 15 minutes and
  groups every 3 hours. Synthetic heartbeat input uses `remember_input=False`,
  while verified proactive assistant output may be persisted to the target
  conversation and global event ledger.

## 2. Provider Request Layout

The persona prompt and five operational prompts live in the standalone
`system-prompts.yaml` file and are selected through `prompts.system_file`.
Runtime, message-summary, summary-merge, and Episode-summary requests load that
same validated file; missing or empty operational levels fail startup.
The mandatory `identity` config block supplies distinct bot and owner IDs, names,
and the owner's relationship alias. Prompt loading resolves their placeholders once.
It is a load-time substitution,
not a runtime actor resolver; current speakers remain identified by event `actor_id`.
`config_manager reload`
reloads the external prompt file. Production stores it under the shared deploy
root so a release does not overwrite operator changes.

Every LLM request has these layers, in order:

1. A provider-native Chinese `system` message containing stable platform rules and the configured persona.
2. The audience-filtered view of the global chronological Life Stream. Human and service inputs use provider `user`, assistant outputs use `assistant`, and completed tool rounds use native `assistant(tool_calls)` plus `tool` messages. The current conversation and explicitly global events are visible; unrelated private conversations remain in the ledger but are not model input. Actor and conversation identity is carried by readable source prefixes.
3. A temporary platform-state `user` message containing current UTC+8 time, runtime flags and active work.
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
  the bounded rewrite loop. When rewrites are exhausted, one complete,
  unambiguous `TO_USER` block may be sent while `TO_SELF` is discarded;
  otherwise a flat-text protocol error is sent for an ordinary visible pipeline
  instead of silently dropping the turn. Strict parsing remains available for validation.
- `TO_USER` is the only ordinary visible channel. It is flat-text gated and is
  sent once. `TO_SELF` is a bounded subjective delta stored in global inner
  journal only after a complete successful turn; protocol tags never enter the
  working window or durable assistant reply.
- Assistant content from a tool-call round is never sent or parsed. DeepSeek
  `reasoning_content` is retained only for the current provider-native loop.
- Pipe-based multi-message splitting is disabled. `|` is sent literally until a
  replacement framing protocol is designed.
- `send` handles special QQ segments and Markdown-rendered images. Its `target`
  is mandatory and must be an explicit `private:<numeric_id>` or
  `group:<numeric_id>` conversation key; nicknames are never resolved by the
  transport tool. Ordinary text for the current conversation uses `TO_USER`;
  text-only `send` is allowed only for an explicitly different target.
- A verified cross-conversation send is stored under a destination-scoped
  logical pipeline identity. The source pipeline, source conversation, and tool
  call remain provenance in the outbound payload and source action ledger, but
  they are not part of the destination Episode group. This prevents destination
  compaction from rehydrating unrelated private source events.
- `status_update` is a short visible progress event for long tools. It executes
  before other calls in its round, does not end the pipeline, and is not added to
  assistant history. A long registered tool without an explicit update gets
  one pipeline-generated fallback; heartbeat pipelines suppress progress events.
- `no_reply` intentionally ends a turn without visible output, but is reserved for genuine silent maintenance or autonomous checks. Unknown facts, refusals, privacy-boundary explanations, and completed user-requested work still require a short `TO_USER` response.
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
protection and writes a verified action result. Historical native tool
projection selects that final committed result instead of the provisional
`staged` response. This preserves turn-level
atomicity when a pipeline is interrupted.

## 5. Working Window And Summaries

Raw events remain the source of truth. For an Event Ledger-backed conversation,
`Episode` is the only persistent projection unit used for history recovery. It
describes a finalized prefix and carries exact `conversation_id` plus ledger
sequence coverage. A request-level budget pass may create an Episode early;
otherwise the projector keeps a newest complete suffix without inventing a
coverage boundary. If the raw-event page limit leaves older uncovered history
outside the projection, the request receives an explicit sequence-based
omission marker; the gap is never presented as complete coverage.

The old `messages/summaries` tables remain a compatibility path for databases
that have no Ledger rows for the current conversation. Their per-message
summaries are descriptive only. Legacy `last_message_id` and
`covered_through_message_id` values never skip Event Ledger records.
Once a conversation has Ledger rows, the legacy summary path is read-only and
is never written by normal pipeline cleanup. A top-level pipeline captures one
Ledger high-water sequence in `PipelineDeps`; initial projection, token
reclaim, and any context rebuild use that same snapshot.
`MessageWindow` is a UI/runtime cache, not a second history authority.
Recovery and compaction operate on complete pipeline turns and never cut a
user/assistant exchange or a native tool round in half.

Startup restores the newest eligible conversation rows in chronological order.
It uses the same eligibility rules as live window insertion and excludes
memory, self-note, action artifacts, cancelled/error turns, empty/no-reply
turns, and malformed records.

If startup restoration is capped and older eligible rows remain outside the
window, that window is marked coverage-untrusted. It may be used by the legacy
compatibility path, but must not advance a coverage boundary; repeated
restoration is preferable to claiming history that was never summarized.

## 6. Token-Aware Context Recovery

Recovery considers the complete provider request: system rules, Life Stream,
platform state, current input, and tool schemas. Before a call it uses a
deterministic estimate; after a call it records the provider's actual
`prompt_tokens` when available. The deterministic fallback counts CJK and
other non-ASCII text conservatively instead of applying an English-only
four-characters-per-token estimate.

Configuration defines model context capacity, trigger ratio, target ratio, and
reserved output tokens. When the request is over the trigger, the same Ledger
projector is rebuilt with a smaller history budget. It may create Episodes from
finalized complete prefixes, then selects either each Episode or the raw events
it covers, never both. Summary input is not silently truncated; if it cannot
fit the summarizer request, it is split into explicit chunks. Current native
tool-loop messages are never compacted mid-loop. A failed summarization leaves
the raw Ledger available and does not advance a boundary.

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
Markdown hash/reference, the temporary generated file reference, and its
durable Media Ledger ID. After a verified send, the image is copied into the
Media Ledger before the renderer PNG is deleted. Failed sends delete the
temporary PNG without creating successful outbound media provenance.

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

The current projector presents finalized events from the unified timeline after
applying the current conversation as the model-input audience. The global
ledger remains one append-first life stream, but a private conversation can see
its own events and events marked `global`; it cannot see the raw text of an
unrelated private conversation. A group has one shared conversation boundary,
so all members see that group's events. All human and service sources use
provider `user`; readable actor and conversation prefixes preserve identity.
`actors` is the global registry for stable display names, private aliases and
relationship labels. Visibility and audience remain attached to every event
for audit and later output gates.

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

## 12. External Service Ingress

The optional ingress is a standard-library asyncio HTTP/1.1 listener bound to
an enforced loopback `ingress.host:ingress.port`, disabled by default. It accepts only
`POST /v1/events` with `Content-Type: application/json` and a matching
`Authorization: Bearer <ingress.token>` header. It enforces a request body
limit and never logs the token.

The request body reuses the NapCat private-message shape. The `user_id` field
is a stable service identifier validated against a safe ASCII identifier
grammar. The ingress maps it to `service:<id>` for `actor_id` and
`conversation_id`; `sender.card` or `sender.nickname` becomes the readable
actor name. The source is therefore a service actor, not a QQ user, while all
service and human events still share the global Life Stream.

Image segments must use an HTTP(S) `data.url` and then follow the normal inbound
media path. Local file paths and non-text/non-image segments are rejected before
persistence. The image is downloaded only when the configured media policy
allows it, then its verified Media Ledger ID is attached to the already accepted
event before the event is finalized.

The configured positive numeric `ingress.target_user_id` is the NapCat private-message target
for service pipeline output. It is deliberately independent from the source
service ID. Only private service events are accepted in this version.

Each accepted event receives a deterministic UUID derived from service ID and
`message_id`, is stored with `status=received` and its original NapCat payload,
then is queued. Duplicate retries are acknowledged without inserting another
event. A service FIFO worker does not cancel earlier reports. On startup,
`received` service events are reconstructed from their stored payload and
queued again; successful or failed processing changes their lifecycle status.
Graceful cancellation restores an in-flight service event to `received` rather
than excluding it from restart recovery.

The listener returns a NapCat-shaped JSON acknowledgement with HTTP `202` after
the event is durably stored and queued. Ingress is local-only by default; a
reverse proxy or tunnel is required for remote producers.

## 13. Output Gate

Only `TO_USER` is subject to the ordinary flat-text gate. Obvious complex
Markdown (headings, tables, code fences, links/images, or LaTeX) is rejected
before sending and a bounded correction is returned to the current model loop.
The model must rewrite it as plain text or use `send.markdown_image`. A rejected
response is never persisted as a sent outbound event, and its `TO_SELF` is not
eligible for inner-journal commit.

## 14. Documentation Ownership

- `docs/current-design.md`: current semantic and architectural baseline.
- `README.md`: installation, configuration, operation, and user-facing behavior.
- `AGENTS.md`: agent workflow and invariants.
- `init.md`: project charter and implementation status.
- `bottle/docs/`: historical design source and archived rationale.

## 15. Production Acceptance

A release is complete only after local and server tests pass, the optional
Markdown renderer check passes, the shared production config is patched without
reformatting unrelated values, and all shared prompts are synchronized atomically
after backing up the previous production prompt file. The default
Markdown renderer timeout is 60 seconds to cover Chromium cold startup. Systemd
must report the service active, NapCat must be
connected, and fresh logs verify text, tool, image, restart restoration, failed
send, and compaction behavior.

## 16. Delivery Groups

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
14. Add authenticated local service ingress with durable acceptance, FIFO
    processing, service identity projection, owner reply routing, and restart
    recovery.

## 17. AI-Native Scheduled Workflows

Status: implemented as the minimal generic scheduler, constrained file tool,
and optional OpenCode delegation path. Five-field cron is supported; arbitrary
workflow graphs are not part of this implementation.

The phase-two workflow model has five generic pieces:

1. The scheduler stores time semantics and a natural-language prompt.
2. Optional Skill files disclose instructions only when a task needs them.
3. The bot decides whether and how to delegate the prompt.
4. OpenCode or another configured task Agent plans and executes the work.
5. A worker-only `report_task` callback returns the result to the original bot
   pipeline.

### 17.1 Scheduler Boundary

The scheduler is a general wake-up mechanism, not a workflow engine. Its target
is bound from the conversation that creates the task. Its persisted payload is a
prompt plus an optional opaque natural-language `context`; the scheduler never
interprets that context. The current timing surface covers one-shot,
fixed-interval, and five-field UTC+8 cron triggers, plus list, pause, resume,
and cancel lifecycle operations. A recurring trigger atomically stores its
next fire time so service restart does not lose the schedule. Missed recurring
slots are skipped rather than replayed as a burst.

When a task becomes due, its prompt enters the existing pipeline with a clear
platform source. It must wait behind an active user pipeline rather than cancel
it. The trigger itself is platform input, not a fabricated human message; the
verified actions, worker report, and any actual bot output remain durable.

Scheduler records must not gain `skill_name`, `target_group`, connector type,
workflow steps, or domain-specific fields. Skill paths and delivery requirements
belong in the prompt or opaque context. Heartbeat keeps its separate
proactive-conversation semantics even if both systems share a low-level wake-up
primitive.

### 17.2 Skill Files

A Skill is a plain text instruction file, not a registered runtime object. A
workflow may use a short bot-facing file and a detailed worker-facing file as a
convention. The short layer only tells the bot when to delegate and which
detailed file OpenCode should read. The detailed layer contains the actual
workflow guidance. Neither layer requires a registry, compiler, state machine,
or scheduler integration.

The scheduled prompt may simply say, for example, to read a particular bot
instruction file. The bot then passes a natural-language task containing the
worker instruction path to the generic delegation tool.

### 17.3 Delegation And Reporting

The bot-facing delegation surface should be one generic operation whose
essential input is a natural-language prompt. It starts a configured OpenCode
run and returns promptly; it does not expose arbitrary command execution to the
bot and does not understand the task's business domain.

Each worker run receives access to a constrained `report_task` tool. Run
identity and the originating conversation are bound by the platform, so the
worker reports only a compact natural-language result. OpenCode is responsible
for calling this tool when its work is complete. A valid callback is idempotent,
marks the run reported, and wakes the original bot pipeline with the report.

Process exit and stdout are diagnostics, not a substitute for `report_task`.
If the worker never calls the tool, the run remains honestly unreported or
times out. The platform must not infer a successful result.

The worker transcript, intermediate files, and tool chatter stay outside the
bot's Life Stream. Only verified status and the compact report return to the bot,
which retains responsibility for persona, memory decisions, and user-facing
communication.

### 17.4 Explicit Non-Goals

- No Science, paper, market, calendar, or other domain connector in the bot
  backend.
- No workflow-step JSON or backend-authored execution graph.
- No Skill-specific scheduler fields or Skill registry.
- No model-selected target user or group in delegation or reporting calls.
- No automatic conversion of worker stdout into a claimed completion report.

The current implementation has completed the generic scheduler lifecycle,
constrained file access, one generic OpenCode delegation operation, and the
bound `report_task` callback. The development machine has been verified with
OpenCode 1.18.10 and the installed plugin SDK: a real stub-provider run
emitted `report_task`, the runner accepted the token-bound report, and the
durable task was marked `reported`. The production server currently has no
OpenCode executable, so production delegation remains disabled until the CLI
and plugin SDK are installed deliberately. Each worker uses isolated XDG and
database paths so a legacy global OpenCode database cannot affect task startup.
