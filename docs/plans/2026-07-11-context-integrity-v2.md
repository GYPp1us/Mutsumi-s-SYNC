# Context Integrity V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the thirteen approved context, persistence, send, image, prompt, configuration, and documentation corrections as one production release.

**Architecture:** Keep the single-function pipeline and scheduler-owned state. Add explicit structured persistence semantics around it: exact record IDs for window compaction, typed summary coverage, and a verified action ledger. Build provider requests from stable system rules, a persistent Context Packet ending in persona, a timestamped window, Runtime Injection, and current input.

**Tech Stack:** Python 3.10-compatible asyncio, Pydantic, aiosqlite, httpx, PyYAML, pytest, NapCat HTTP/WebSocket, optional Node/Playwright renderer.

---

### Task 1: Documentation Baseline

**Files:** `docs/current-design.md`, `README.md`, `AGENTS.md`, `init.md`

- [ ] Establish the canonical current design and thirteen delivery groups.
- [ ] Remove stale output and prompt protocol claims from public docs.
- [ ] Mark `bottle/docs/` as historical and link the canonical design.
- [ ] Commit the documentation baseline.

### Task 2: Send Truthfulness And Literal Content

**Files:** `src/mutsumi_sync/message/sender.py`, `src/mutsumi_sync/tools/send.py`, `src/mutsumi_sync/pipeline.py`, `tests/test_send_tool.py`, `tests/test_pipeline_e2e.py`

- [ ] Add failing tests for NapCat `failed`, malformed responses, and literal pipes.
- [ ] Normalize sender results into a verified success predicate/error result.
- [ ] Persist visible replies and image artifacts only after verified success.
- [ ] Send final assistant content once, with `|` untouched.

### Task 3: Local Config Editing And Persona Config

**Files:** `src/mutsumi_sync/config.py`, `src/mutsumi_sync/tools/config_manager.py`, `config.example.yaml`, `tests/test_config.py`, `tests/test_config_manager.py`

- [ ] Add failing tests for depth-three edits, comment/order preservation, strict booleans, and persona loading.
- [ ] Implement indentation-aware arbitrary-depth scalar replacement/insertion.
- [ ] Add `prompts.persona` with backward-compatible loading of legacy `system_prompt` as persona.
- [ ] Keep stable platform instructions code-owned.

### Task 4: Context Layout And Canonical Prompt

**Files:** `src/mutsumi_sync/pipeline.py`, `tests/test_context.py`, `tests/test_pipeline_e2e.py`

- [ ] Add failing tests for one non-empty system message and persona at Context Packet tail.
- [ ] Remove the handwritten tool inventory and pipe protocol from system rules.
- [ ] State schema authority, runtime timestamp semantics, staged writes, and verified actions.
- [ ] Preserve DeepSeek reasoning only in the current native tool loop.

### Task 5: Summary Types And Exact Coverage

**Files:** `src/mutsumi_sync/memory/store.py`, `src/mutsumi_sync/memory/window.py`, `src/mutsumi_sync/pipeline.py`, `tests/test_store_memory.py`, `tests/test_window.py`, `tests/test_pipeline_e2e.py`

- [ ] Add schema migration tests for summary kind and nullable trusted coverage.
- [ ] Carry message record IDs on window entries.
- [ ] Make per-message summaries non-covering and compaction summaries exact.
- [ ] Compact only complete record-ID turns and remove silent summary truncation.
- [ ] Treat legacy coverage as untrusted.

### Task 6: Clean Startup Restoration

**Files:** `src/mutsumi_sync/memory/store.py`, `src/mutsumi_sync/scheduler.py`, `tests/test_scheduler_smoke.py`

- [ ] Add failing mixed-category/status restoration tests.
- [ ] Query newest eligible conversation records, then restore chronologically.
- [ ] Skip records covered by trusted compaction summaries.
- [ ] Exclude memory, priority, artifacts, cancelled/error/empty/no-reply records.

### Task 7: Cancellation-Safe Staged Writes

**Files:** `src/mutsumi_sync/pipeline.py`, `tests/test_pipeline_e2e.py`

- [ ] Add cancellation tests proving each staged write commits exactly once.
- [ ] Change immediate results from `queued` to an explicit `staged` contract.
- [ ] Shield cleanup flushes from cancellation and capture their verified results.
- [ ] Do not change staged writes to immediate writes.

### Task 8: Actual Tool Failure Counting

**Files:** `src/mutsumi_sync/pipeline.py`, `tests/test_pipeline_e2e.py`

- [ ] Add tests for three actual errors, successful reset, and repeated successes.
- [ ] Count `[Error: ...]` results per consecutive same-tool run.
- [ ] Reject the fourth call after three failures with a clear tool result.

### Task 9: Verified Action Ledger

**Files:** `src/mutsumi_sync/memory/store.py`, `src/mutsumi_sync/pipeline.py`, `tests/test_store_memory.py`, `tests/test_pipeline_e2e.py`

- [ ] Add a structured `actions` table and CRUD tests.
- [ ] Record final tool/send success, call ID, sanitized arguments, and result.
- [ ] Record image message ID, Markdown hash/reference, and generated file.
- [ ] Inject a bounded recent-action Context Packet section.
- [ ] Remove sent-image assistant markers from replies and windows.

### Task 10: Images Through The Normal Pipeline

**Files:** `src/mutsumi_sync/message/classifier.py`, `src/mutsumi_sync/scheduler.py`, `src/mutsumi_sync/pipeline.py`, `tests/test_classifier.py`, `tests/test_pipeline_e2e.py`

- [ ] Add tests preserving image captions and metadata.
- [ ] Produce a synthetic user input from caption, vision description/error, and artifact reference.
- [ ] Persist structured image input on the normal inbound lifecycle record.
- [ ] Run normal context, tools, final content, cancellation, archive, and window logic.

### Task 11: Request-Level Token Budget

**Files:** `src/mutsumi_sync/config.py`, `src/mutsumi_sync/pipeline.py`, `config.example.yaml`, `tests/test_config.py`, `tests/test_pipeline_e2e.py`

- [ ] Add capacity, trigger, target, reserve, and last-provider-usage configuration/tests.
- [ ] Estimate full messages plus tool schemas before the first call.
- [ ] Compact exact oldest turns until the target budget is met.
- [ ] Use provider `prompt_tokens` as observed telemetry after each call.
- [ ] Never compact native messages during the current tool loop.

### Task 12: Documentation Synchronization

**Files:** `README.md`, `AGENTS.md`, `init.md`, `docs/current-design.md`, `config.example.yaml`

- [ ] Synchronize implemented configuration, context, image, ledger, and recovery behavior.
- [ ] Remove stale claims about Dashboard/Tester fixes and pipe splitting.
- [ ] Document migration compatibility and production checks.

### Task 13: Verification And Production Release

**Files:** `scripts/release_to_production.ps1`, production shared config

- [ ] Run focused tests after each task and full local pytest.
- [ ] Run `npm run check` for the Markdown renderer.
- [ ] Inspect diff for secrets, unrelated churn, and Python 3.10 incompatibilities.
- [ ] Commit in reviewable groups, push, create and merge PR.
- [ ] Back up and locally patch only production prompt/persona/context keys.
- [ ] Deploy with the release script and run server tests.
- [ ] Verify systemd, NapCat, fresh logs, text/image sends, failed send handling, restart restoration, and compaction.
