# Mutsumi's SYNC — Architecture for AI Agents

> 本文档供 LLM / AI Agent 消费。使用结构化描述、精确类型签名和最小化自然语言。

---

## 1. PROJECT IDENTITY

- **Name**: Mutsumi's SYNC
- **Purpose**: QQ chatbot via NapCat WebSocket + OpenAI-compatible LLM
- **Runtime**: Python 3.11+, asyncio
- **Entry**: `start.py` (creates Scheduler → starts WS receiver)
- **No TUI assumed** — TUI is external observer, not part of core architecture

---

## 2. TOPOLOGY

```
main()
  └── PipelineScheduler.__init__(deps)
        ├── .config: Config                    ← GLOBAL (all users share)
        ├── .registry: ToolRegistry            ← GLOBAL
        ├── .skills: SkillRegistry             ← GLOBAL
        ├── .schedule: ScheduleEngine          ← GLOBAL
        ├── .llm_factory: (Config) -> LLMProvider
        ├── .sender: MessageSender
        ├── .store: MemoryStore
        ├── ._windows: dict[str, MessageWindow]   ← PER-USER
        ├── ._sessions: dict[str, SessionState]    ← PER-USER
        └── ._tasks: dict[str, asyncio.Task]       ← PER-USER

  main() then:
    receiver = MessageReceiver(config.napcat)
    receiver.on_message(scheduler.dispatch)
    await receiver.run()

PipelineScheduler.dispatch(event) → creates/overwrites Task for user_key
PipelineScheduler.on_scheduled(task) → creates Task with synthetic message

IMPORTANT: Config, ToolRegistry, Skills, ScheduleEngine are GLOBAL singletons.
  A config change by any user affects ALL users immediately.
  A skill loaded by any user becomes available to ALL users.
  This is the intended behavior for a single-owner QQ bot.
```

---

## 3. CORE FUNCTION: `pipeline()`

```python
async def pipeline(
    # ── Input ──
    message: str,
    msg_type: MessageType,
    image_md5: str | None,

    # ── Dependencies (all injected by Scheduler) ──
    *,
    config: Config,                    # GLOBAL mutable, canonical
    registry: ToolRegistry,            # GLOBAL mutable
    skills: SkillRegistry,             # GLOBAL mutable
    schedule: ScheduleEngine,          # GLOBAL mutable
    llm_factory: Callable[[Config], LLMProvider],
    sender: MessageSender,
    store: MemoryStore,
    window: MessageWindow,             # PER-USER, Scheduler-owned
    session: SessionState,             # PER-USER, Scheduler-owned
    peer: Peer,
) -> None:
    """
    Process one message end-to-end.
    Can be cancelled via asyncio.Task.cancel() at any await point.
    Cancellation propagates through httpx → LLM API connection drops.
    """
```

### 3.1 Internal Flow (pseudocode — reference only, implementers may reorder steps)

```
PIPELINE(message, *, deps):
    1. CLASSIFY:
       if IMAGE → handle image path (possibly consult meme cache)
       if not TEXT → return early

    2. DEDUP:
       if session.has_pending() → cancel old task, return
       session.mark_pending()

    3. COLD START:
       if session.is_cold(config.session.timeout) → sender.send_poke(peer)
       session.touch()

    4. VECTOR (optional shortcut for SHORT_TEXT):
       if msg_type == SHORT_TEXT and vector_available():
           hits = vector.search(embed(message), top_k=3)
           if hits and hits[0].score > config.vector.threshold:
               sender.send(peer, hits[0].text)
               window.add(user=message, bot=hits[0].text)
               return

    5. CONTEXT:
       context = window.get_context()

    6. TOOL LOOP:
       tools = registry.snapshot()                  # snapshot with version
       tools_version = registry.version
       system_msg = build_system_prompt(config, skills)
       messages = [system_msg, context..., HumanMessage(message)]

       llm = llm_factory(config)

       for step in 0..MAX_TOOL_CALLS:
           response = await llm.chat(messages, tools=tools)
           # ↑ CANCELLATION POINT: Task.cancel() interrupts here

           if not response.tool_calls:
               break

           for tc in response.tool_calls:
               result = registry.execute(tc.name, tc.args)

               # ── SELF-MODIFICATION DETECTION ──
               if registry.version != tools_version:
                   tools = registry.snapshot()
                   tools_version = registry.version
               if config.dirty:
                   llm = llm_factory(config)
                   config.dirty = False
               if skills.dirty:
                   system_msg = build_system_prompt(config, skills)
                   messages[0] = system_msg
                   skills.dirty = False

               messages.append(ToolMessage(content=result))

       # ── SEND ──
       sender.send(peer, response.content)
       window.add(user=message, bot=response.content)

    7. CLEANUP:
       session.touch()
```

---

## 4. MUTATION TRACKING (the self-referential bridge)

### 4.1 Constraint

> Tools may modify global state (config, tool registry, skills).
> Such modifications MUST be visible to the SAME pipeline invocation
> on the next LLM call (next iteration of the tool loop).
>
> **Implementation mechanism is not prescribed.** One possible approach:

### 4.2 Version Counter Pattern (recommended)

Uses a monotonic counter instead of a bool to avoid cross-pipeline race conditions:

```python
class ToolRegistry:
    _tools: dict[str, Tool]
    version: int = 0          # monotonic counter, incremented on mutation

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        self.version += 1

    def remove(self, name: str):
        del self._tools[name]
        self.version += 1

    def snapshot(self) -> tuple[list[Tool], int]:
        """Returns (current tools, current version). Does NOT reset version."""
        return list(self._tools.values()), self.version

class Config:
    dirty: bool = False

    def set(self, key: str, value: Any):
        # dot-path traversal: "model.temperature" → self.model.temperature = value
        self.dirty = True

class SkillRegistry:
    dirty: bool = False        # True when loaded skills or system_prompt changed

    def load(self, name: str) -> list[Tool]:
        """Loads skill. Returns new tools. Sets dirty flag."""
```

### 4.3 Pipeline comparison rule

```
After EACH tool execution in the tool loop:
  IF registry.version != tools_version → resnapshot tools
  IF config.dirty → rebuild LLM client, reset dirty
  IF skills.dirty → rebuild SystemMessage, reset dirty

Why version counter over bool:
  Bool is shared across all concurrent pipelines. Pipeline A sets dirty=True,
  Pipeline B calls snapshot() which resets dirty=False — Pipeline A never sees it.
  A monotonic counter eliminates this race: each pipeline remembers its own
  snapshot version and compares against the current registry version.
```

---

## 5. DATA STRUCTURES

### 5.1 Tool

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict              # JSON Schema
    handler: Callable[[dict], Awaitable[str]]
    source: str                   # "builtin" | skill_name
```

**Constraint**: `handler` is an async callable receiving `(tool_args: dict) → str`.
The specific handler type (builtin function, HTTP call, scheduler action, MCP proxy)
is an **implementation decision**, not an architecture constraint.

### 5.2 Skill

Skills are extensions that add tools and optional system prompt fragments.
The **loading format** (YAML file, Python module, JSON) is an implementation decision.

```python
@dataclass
class Skill:
    name: str
    description: str
    system_prompt: str           # appended to SystemMessage when loaded
    tools: list[Tool]
```

### 5.3 ScheduledTask

```python
@dataclass
class ScheduledTask:
    id: str
    user_id: str
    group_id: str | None
    schedule: str                # cron expression at minimum; other formats optional
    prompt: str                  # fed to pipeline as message on trigger
    context: dict                # extra context injected into pipeline
    enabled: bool
    created_at: float
```

### 5.4 MessageWindow

```python
class MessageWindow:
    """Sliding window of recent conversation messages, configurable max size."""
    def add(self, user_id, content, is_bot=False)
    def get_context(self) -> list[str]
```

### 5.5 SessionState

```python
@dataclass
class SessionState:
    last_active: float = 0.0
    is_pending: bool = False     # True when pipeline is processing

    def is_cold(self, timeout: float) -> bool
    def touch(self)
```

### 5.6 Peer

```python
@dataclass
class Peer:
    chat_type: int  # 1=private, 2=group
    peer_uid: str
```

---

## 6. BUILT-IN TOOL MANIFEST

| Tool | mutates | description |
|------|---------|-------------|
| `config_manager` | Config | get/set/list/reload config |
| `skill_manager` | ToolRegistry + Skills | load/unload/list skills |
| `scheduler` | ScheduleEngine | register/cancel/list scheduled tasks |
| `memory` | (read-only) | recall/search/summarize conversation history |
| `system` | (read-only) | status/health/uptime/tasks |
| `http_api_call` | (none) | arbitrary HTTP call |

---

## 7. SCHEDULER — CORE ORCHESTRATOR

```python
class PipelineScheduler:
    # ── Canonical State (GLOBAL, shared by all user pipelines) ──
    config: Config
    registry: ToolRegistry
    skills: SkillRegistry
    schedule_engine: ScheduleEngine
    llm_factory: Callable[[Config], LLMProvider]
    sender: MessageSender
    store: MemoryStore

    # ── Per-User State ──
    _windows: dict[str, MessageWindow]
    _sessions: dict[str, SessionState]
    _tasks: dict[str, asyncio.Task]

    async def dispatch(self, event: MessageEvent):
        """
        1. Compute key = f"{group_id}:{user_id}"
        2. Cancel existing _tasks[key] if any
        3. Ensure _windows[key] and _sessions[key] exist
        4. task = asyncio.create_task(pipeline(msg, *, deps))
        5. _tasks[key] = task
        """

    async def execute_scheduled(self, task: ScheduledTask):
        """
        Called by ScheduleEngine on trigger.
        Creates synthetic message and dispatches through pipeline.
        """
        synthetic = SyntheticEvent(
            user_id=task.user_id,
            group_id=task.group_id,
            raw_message=f"[SCHEDULED:{task.id}] {task.prompt}",
        )
        await self.dispatch(synthetic)

    def _make_key(self, event) -> str
    def _make_deps(self, key) -> PipelineDeps
    # PipelineDeps bundles all injected dependencies for pipeline()
```

---

## 8. CANCELLATION CONTRACT

```
Pipeline is wrapped in asyncio.Task.
Scheduler calls task.cancel() when new message arrives for same user.

Contract:
  - pipeline() MUST propagate CancelledError (no bare `except:`)
  - All await points inside pipeline MUST be cancellable (no sync blocking I/O)
  - Window, Session, Store are owned by Scheduler → survive cancellation
  - LLM http request via httpx → connection drops on cancel → API may charge
    for already-processed tokens (provider-dependent behavior)
  - Sender.send() for summaries → fire-and-forget, may complete after cancellation
```

---

## 9. ERROR HANDLING

```
pipeline() contract:
  - All exceptions caught at top level → logged → returned as error string to user
  - CancelledError: re-raised (NOT caught)
  - Tool execution errors: returned as ToolMessage with "[Error: ...]" prefix
  - LLM API errors: returned as "[模型暂时不可用: ...]" to user
  - Sender errors: logged, user sees "[发送失败]"
```

---

## 10. CODE ORGANIZATION (constraint only)

```
Code MUST be organized by responsibility layer:
  message/     — I/O: receive from WS, send via HTTP, classify messages
  tools/       — Tool implementations (pure functions receiving (args, *, deps))
  memory/      — State: sliding window, long-term store, vector index
  schedule/    — Schedule engine
  provider/    — LLM abstraction

Source layout and file naming are implementation decisions.
```

---

## 11. INVARIANTS

1. **pipeline() is a stateless async function** — all state accessed via injected parameters; no global state access; no class methods on shared mutable objects
2. **All mutable state lives in Scheduler** — pipeline receives references, Scheduler owns canonical copies
3. **Tool execution = f(args, *, deps) → str** — synchronous where possible, async for I/O tools
4. **Global state mutations within a tool call MUST be visible to the same pipeline invocation on the next LLM call** — implementation mechanism (version counter, dirty flag, resnapshot) is not prescribed
5. **Cancellation = Task.cancel()** — no custom cancellation protocol needed
6. **Skill loading MUST NOT execute arbitrary code at load time** — skill definitions are data, not code
7. **Scheduled tasks = synthetic messages** — enter pipeline identically to user messages
8. **Config, ToolRegistry, Skills, ScheduleEngine are GLOBAL** — changes affect all users

---

## 12. CONFIG CATEGORIES (minimum required)

```yaml
napcat:
  ws_url: str
  http_url: str
  access_token: str

model:
  provider: str
  model: str
  temperature: float
  api_key: str
  base_url: str | null

context:
  window_size: int
  max_tokens: int

session:
  timeout: int     # seconds

# Additional categories (vector, dedup, cache, skills) are implementation-defined.
# Specific field names, default values, and nesting structure are implementation decisions.
```
