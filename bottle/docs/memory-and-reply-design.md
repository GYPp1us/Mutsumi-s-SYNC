# 记忆系统与回复机制 — 完整设计方案

**版本**: v1.0
**日期**: 2026-06-22
**状态**: Phase 2 设计，待实现

---

## 0. 术语定义 / Terminology

| 中文 | English | 定义 |
|-----------|---------|-------------|
| 工作记忆 | Working Memory | 最近 N 条原始消息，随每次请求注入 contexts，增长式窗口 |
| 摘要归档 | Summary Archive | 长消息（>100 tokens）的固定摘要文本，跨轮次复用，注入 context 前缀 |
| 长期记忆 | Long-term Memory | SQLite FTS5 全文索引的事实/印象记录，通过 tool_call 按需检索 |
| 模型私思 / 私人笔记 | Self-Note | 模型对被使用者的私密印象 / 自我调节笔记，自动注入 context，不由用户可见 |
| 防抖 | Debounce | 调度器层面，在短时间窗内累积消息后在一次性合并后 dispatch |
| 合并消息 | Merged Message | 防抖窗口内的多条用户消息拼接为一条输入的文本 |
| 上下文阈值 | Context Threshold | 工作记忆和摘要各有一对 max/min 参数控制容量，超出 max 时触发折半回收 |
| 动态窗口回收 | Dynamic Window Recycling | 增长式窗口——新内容追加到末尾不丢弃，仅在超过 max 时从头部批量回收，回收后剩余约 min |
| 前缀稳定性 | Prefix Stability | 消息数组的前缀在连续请求中保持字节级不变，从而命中 LLM API 前缀缓存（prefix caching） |
| 归档 | Archival | pipeline 结束后对长消息触发摘要生成，写入 summaries 表 |
| 混合回复 | Hybrid Reply | LLM 输出同时支持纯文本（content 字段）和 `send` tool_call，前者为默认路径 |

本定义应用于所有后续设计与代码注释。

---

## 1. 综述

```
                    ┌─────────────┐
                    │  NapCat WS  │
                    └──────┬──────┘
                           │ MessageEvent
                    ┌──────▼──────────────────────────────────────┐
                    │          PipelineScheduler                   │
                    │                                              │
                    │  dispatch(event)                             │
                    │    └→ 防抖窗口 (per-user, 1.5s)               │
                    │         └→ 合并消息 → cancel_user → pipeline │
                    │                                              │
                    │  _pending_events: dict[key, list[Event]]     │
                    │  _debounce_timers: dict[key, Task]           │
                    └──────┬──────────────────────────────────────┘
                           │
                    ┌──────▼──────────────────────────────────────┐
                    │             pipeline()                       │
                    │                                              │
                    │  输入: merged_message + deps                 │
                    │                                              │
                    │  1. 构建上下文 (build_context)                │
                    │  2. LLM 调用 (含工具循环)                     │
                    │  3. 回复 (混合模式: content || send tool)     │
                    │  4. 归档检查                                  │
                    └──────┬──────────────────────────────────────┘
                           │ 结束后
                    ┌──────▼──────────────────────────────────────┐
                    │           后处理（pipeline 结束后）           │
                    │                                              │
                    │  * 归档: 长消息 → 摘要 → summaries 表         │
                    │  * 私思: LLM 通过 memory_save 主动写入        │
                    │  * 截断: summaries > 180 → 删除最旧          │
                    └─────────────────────────────────────────────┘
```

---

## 2. 上下文组装 / Context Assembly

每条请求发送给 LLM 的 `messages` 数组构成如下：

```
messages = [
  ┌─ SYSTEM_PROMPT              ← 固定配置
  ├─ [私人印象 — current:450/target:1000 tokens]
  │   * current_self_note        ← 单条全文，≤1000 tokens。附带长度元数据
  ├─ [/私人印象]
  ├─ TOOLS_SCHEMA               ← registry.to_openai_schema()
  │
  ├─ [摘要]                     ← 来自 summaries 表，每条 100 tokens 内
  │   * summaries[0..179]       ← 按 seq 升序，固定文本，前缀稳定
  ├─ [/摘要]
  │
  ├─ [对话记录]                  ← 来自 MessageWindow，增长式窗口
  │   * window.get_context()    ← 超过 window_max_tokens 时触发回收
  ├─ [/对话记录]
  │
  └─ {"role": "user", "content": merged_message}  ← 防抖合并后的当前输入
]
```

**行为约束**：
- `self_note` 与 `summaries` 是固定文本。一旦写入，后续请求复用同一段文本，不再重生成。
- `self_note` 为单条全文，≤ `self_note_target_tokens` tokens。注入时附带当前长度和目标长度元数据。超出 `target * self_note_max_multiplier` 时保护性截断。
- `summaries` 按 `seq` 升序排列，新的摘要追加到末尾（前缀不变）。
- 当 `summaries` 超过 `summaries_max_count` 时，从头部删除最旧的一批，使剩余 ≈ `summaries_min_count`。
- 当 `window` 总 token 达到 `window_max_tokens` 时触发回收（见下节）。

### 2.1 动态窗口回收 / Dynamic Window Recycling

**设计动机**：常规滑动窗口（固定大小，弃旧纳新）导致连续两次请求的消息前缀变化 → API 缓存每请求从头计算。增长式 + 批量回收保持前缀稳定，最大化缓存命中。

**核心参数（Config）**：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `window_max_tokens` | `100000` | 工作记忆触发回收的 token 上限 |
| `window_min_tokens` | `50000` | 回收后工作记忆保留的 token 目标值（≈ max / 2） |
| `summaries_max_count` | `180` | 摘要表触发回收的条目上限 |
| `summaries_min_count` | `90` | 回收后摘要表保留的条目目标值（≈ max / 2） |

**工作记忆回收算法**：

```
pipeline 结束后执行 _recycle_if_needed(window, deps):

  total = estimate_tokens(window.get_context())

  if total <= window_max_tokens:
      return                         # 无需回收

  # 1. 确定回收边界
  #    从窗口头部开始累计 token，直到剩余部分 <= window_min_tokens
  accumulated = 0
  cutoff_idx = 0
  for i, msg in enumerate(window):
      accumulated += estimate_tokens(msg)
      if (total - accumulated) <= window_min_tokens:
          cutoff_idx = i + 1
          break

  to_archive = window[:cutoff_idx]   # 待回收的消息切片
  rest = window[cutoff_idx:]         # 保留的消息（≈ min_tokens）

  # 2. 生成摘要（独立 LLM 调用，低规格模型 summarizer）
  summary_text = await _generate_summary(to_archive, deps)
  #    → 输出 1-2 句固定文本，标记 user/bot 来源和时间范围

  # 3. 摘要写入 summaries 表（固定文本，不再变更）
  await deps.store.add_summary(
      group_key=deps.group_key,
      source="mixed",
      summary=summary_text,
  )

  # 4. 替换窗口内容
  window.replace(rest)               # MessageWindow 新增 replace() 方法

  # 5. 上限保护（摘要表回收）
  await deps.store.trim_summaries(
      deps.group_key,
      max_count=config.context.summaries_max_count,
      min_count=config.context.summaries_min_count,
  )
```

**缓存效果示意**：

```
请求 N:   [SYSTEM][TOOLS][摘要A][摘要B][窗口 msg_a..msg_z]
                                                ← 全部 26 条消息
请求 N+1: [SYSTEM][TOOLS][摘要A][摘要B][窗口 msg_a..msg_z, msg_{z+1}]
                                                ← 追加一条到末尾
  ↑ 前缀 msg_a..msg_z 完全相同 → API 缓存命中 ✓

请求 M (回收后):
请求 M-1: [SYSTEM][TOOLS][摘要A..摘要F][窗口 msg_0..msg_200]
请求 M:   [SYSTEM][TOOLS][摘要A..摘要G][窗口 msg_150..msg_200]
  ↑ 新摘要G追加到 summaries 尾部，窗口从头部削减。前缀变动最小化 ✓
```

**为何折半**：如果每次仅回收 1 条消息（像传统滑动窗口），前缀每条请求变化一条，缓存永远不命中。如果回收比例太小（如 100k → 95k），回收频率过高（几乎每条消息都触发）。折半在"缓存命中时段长度"和"回收频率"之间取得平衡——平均每 50k tokens 约 100-250 轮对话才回收一次。

**摘要表回收规则**：
- 摘要表同样采用增长式 + 批量回收：新摘要追加到末尾，不修改已有前缀。
- 当条目数超过 `summaries_max_count` 时，从头部删除最旧的一批，使剩余 ≈ `summaries_min_count`。
- 回收的摘要直接删除（摘要已是压缩形式，不再二次压缩）。
- 折半理由与工作记忆相同——批量删除、低频触发，保持前缀稳定以命中缓存。

---

## 3. 记忆层级

| 层级 | 名称 | 存储 | 上限 | 注入方式 |
|------|------|------|------|----------|
| L0 | 工作记忆 / Working Memory | `MessageWindow`（内存 deque） | 增长式，`window_max_tokens` 触发回收，回收后 ≈ `window_min_tokens` | 每次请求自动注入 |
| L1 | 摘要归档 / Summary Archive | `summaries` 表（SQLite） | 增长式，`summaries_max_count` 触发回收，回收后 ≈ `summaries_min_count` | 每次请求自动注入 |
| L2 | 长期记忆 / Long-term Memory | `messages` 表（SQLite，FTS5） | 无固定上限 | LLM 通过 `memory_search` tool 主动检索 |
| L3 | 模型私思 / Self-Note | `messages` 表（SQLite，category="self_note"） | 单条全文，≤1000 tokens | 每次请求自动注入全文；LLM 通过 `self_note` tool 主动管理（add / replace） |

---

## 4. 防抖系统 / Debounce System

### 4.1 调度器改动

`scheduler.py` 新增两个字典：

```
_pending_events: dict[str, list[MessageEvent]]
_debounce_timers: dict[str, asyncio.Task[None]]
```

### 4.2 流程

```
MessageEvent 到达:
  1. key = _make_key(event)
  2. 将 event 追加到 _pending_events[key]
  3. 若 _debounce_timers[key] 存在 → 取消旧 timer
  4. 创建新 timer: asyncio.create_task(_debounce_expire(key))

_debounce_expire(key):
  1. await asyncio.sleep(DEBOUNCE_TIMEOUT)  # 可配置，默认 1.5s
  2. events = _pending_events.pop(key, [])
  3. 若 events 非空 → _dispatch_merged(key, events)

_dispatch_merged(key, events):
  1. await cancel_user(key)          # 取消正在运行的 pipeline
  2. _ensure_user_state(key)
  3. 合并 events 为一条消息
  4. 创建 deps → asyncio.create_task(pipeline(...))
```

### 4.3 防抖与 /break 交互

`cancel_user()` 调用时同步清理`_pending_events[key]` 和 `_debounce_timers[key]`。

### 4.4 合并规则

```python
def _merge_events(events: list[MessageEvent]) -> str:
    texts = []
    for event in events:
        classified = classify_message(event.message, event.raw_message)
        if classified.content:
            texts.append(classified.content)
    return "\n".join(texts)
```

- IMAGE/MEDIA 类型直接退出防抖，立即 dispatch 而不等待。
- 图片不参与合并（单独处理，走当前 `pipeline()` 中的 IMAGE/MEDIA 分支）。

---

## 5. 回复机制 / Reply Mechanism（混合模式）

### 5.1 双路径

```
_call_llm 返回后:
  ├─ response.tool_calls 包含 "send" → 执行 send_tool → 发送回复
  ├─ response.tool_calls 包含其他 tool → 进入工具循环（第 6 节）
  ├─ response.content 有文本 → 走 content 路径 → sender.send(peer, content)
  └─ 都不存在 → 异常处理，不能静默
```

### 5.2 `send` Tool 定义

```python
SEND_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "text":     {"type": "string", "description": "纯文本回复"},
        "image":    {"type": "string", "description": "图片 file 路径"},
        "image_url":{"type": "string", "description": "图片 URL"},
        "face":     {"type": "integer","description": "QQ 小表情 ID"},
        "at_user":  {"type": "string", "description": "QQ 用户 ID，群聊 @人"},
        "reply_to": {"type": "integer","description": "引用消息的 message_id"},
        "forward":  {"type": "string", "description": "转发消息 ID"},
    },
    # 无 required 字段 — 至少有文本或图片才是有效调用
}
```

**执行逻辑**：

```python
async def send_tool(args: dict, *, sender, peer) -> str:
    segments = []
    if args.get("text"):
        segments.append({"type": "text", "data": {"text": args["text"]}})
    if args.get("image"):
        segments.append({"type": "image", "data": {"file": args["image"]}})
    if args.get("image_url"):
        segments.append({"type": "image", "data": {"url": args["image_url"]}})
    if args.get("face"):
        segments.append({"type": "face", "data": {"id": str(args["face"])}})
    if args.get("at_user"):
        segments.append({"type": "at", "data": {"qq": args["at_user"]}})
    if args.get("reply_to"):
        segments.append({"type": "reply", "data": {"id": str(args["reply_to"])}})
    if args.get("forward"):
        segments.append({"type": "forward", "data": {"id": args["forward"]}})
    if segments:
        result = await sender.send(peer, segments)
        return json.dumps(result)
    return "[Error: send tool called with no content]"
```

### 5.3 LLM 自主沉默判断

混合回复的适用范围：如果 LLM 判断当前消息不完整、不需要回复，它可以**同时不输出 content 也不调用 send**。pipeline 的`finally` 块仍然清理`is_pending`，不发送任何回复。兜底规则：如果 `session.is_cold()` 则不允许沉默（避免 ghosting）。

### 5.4 多段消息与进度输出

一个 LLM 响应中可以包含**多个 `send` tool_call**。pipeline 按出现顺序逐一执行，实现文本 + 图片 + 表情的组合回复。

当 `send` 与 其他 tool（如 `http_api_call`）同时出现时，工具循环**不终止**——`send` 作为即时反馈发给用户，其他 tool 执行完毕后进入下一轮。这支持"先告诉你我在做什么，查完数据后再告诉你结果"的渐进式对话。详见 6.1 节终止规则。

---

## 6. 工具循环 / Tool Call Loop

### 6.1 循环逻辑

```python
messages = build_context(merged_message, window, summaries, self_notes)
tools_snapshot = registry.snapshot()
llm = llm_factory(config)

send_count = 0

for step in range(MAX_TOOL_STEPS):
    response = await llm.chat(messages, tools=tools_snapshot.tools)

    # 无 tool_calls 且无 content → 沉默（LLM 判断不回复）
    if not response.tool_calls and not response.content:
        break

    # 无 tool_calls 但有 content → content 直发（混合回复路径）
    if not response.tool_calls:
        await sender.send(peer, response.content)
        break

    # 分离 send 和其他 tool
    send_calls = [tc for tc in response.tool_calls if tc.name == "send"]
    other_calls = [tc for tc in response.tool_calls if tc.name != "send"]

    # 执行 send（即时反馈用户）
    for tc in send_calls:
        if send_count >= MAX_SENDS_PER_LOOP:
            break
        result = await send_tool(tc.args, ...)
        send_count += 1

    # 执行其他 tool（读操作直接执行，写操作推入延迟队列）
    for tc in other_calls:
        if tc.name in WRITE_TOOLS:
            _pending_writes.append(lambda: registry.execute(tc.name, tc.args))
            result = "[OK] queued"
        else:
            result = await registry.execute(tc.name, tc.args)

        messages.append({"role": "assistant", "tool_calls": [tc]})
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 终止条件：无 other_calls → 结束
    if not other_calls:
        break
```

**发送后继续规则**：

| 本轮 tool_calls 组合 | 行为 |
|----------------------|------|
| 仅有 send | `send` 执行完成 → break（对话结束） |
| send + 其他 tool | `send` 执行 → 其他 tool 执行 → **继续下一轮** |
| 仅有其他 tool | 执行 → 继续下一轮 |
| 无 tool_calls，有 content | content 直发 → break |
| 无 tool_calls，无 content | break（沉默） |

**保护**：`MAX_SENDS_PER_LOOP`（建议 5）。防止 LLM 无限输出消息流。超出时静默截断。

**典型场景**：

```
User: "查杭州天气，并提醒我下午开会"

Round 1:
  LLM → send("正在查杭州天气...") + http_api_call(weather)
  → 发送反馈，继续循环

Round 2:
  LLM → send("杭州 25 度晴天") + scheduler(reminder)
  → 结果已发，提醒已注册

Round 3:
  LLM → send("都搞定了！")
  → break
```

### 6.2 数据安全 / Write Consistency（后更新设计）

**问题**：工具循环中的 CRUD 操作（`self_note`、`memory_save` → 写 DB、`config_manager set` → 写文件）如果被 `Task.cancel()` 中断，可能产生半提交状态。

**设计**：将所有副作用延迟到 `finally` 块一次性批量提交。`finally` 不响应取消信号，保证要么全部完成要么全部不执行。

```python
async def pipeline(message, *, deps):
    _pending_writes: list[Callable[[], Awaitable[None]]] = []

    try:
        deps.session.mark_pending()
        # cold start ...
        # context building ...

        for step in range(MAX_TOOL_STEPS):
            response = await llm.chat(...)            # ← 取消点
            if not response.tool_calls:
                break

            for tc in response.tool_calls:
                if tc.name in WRITE_TOOLS:             # self_note, memory_save, config_manager set
                    _pending_writes.append(
                        lambda: registry.execute(tc.name, tc.args)
                    )
                    result = "[OK] queued"             # 即时返回假确认
                elif tc.name == "send":
                    result = await send_tool(tc.args, ...)
                else:                                  # 纯读 tool
                    result = await registry.execute(tc.name, tc.args)

                messages.append(ToolMessage(...))

            # 终止判断（见下节）

    except asyncio.CancelledError:
        raise                                          # 不执行任何写入，直接退出

    finally:
        deps.session.clear_pending()

        # ── 不可取消区：批量提交所有写入副作用 ──
        for write in _pending_writes:
            try:
                await write()
            except Exception:
                logger.exception("Post-write failed")

        # 归档 & 回收
        await _archive_long_messages(...)
        await _recycle_if_needed(...)
```

**安全性**：`Task.cancel()` 只发送异步取消信号。如果 task 已在 `finally` 块中执行（同步或 await），cancel 不会中断它。新的 task 需要等当前 task 的 `finally` 完全退出后才启动（由 `cancel_user` 中的 `await task` 保证）。因此 `finally` 内的写入序列是原子执行的。

**假确认处理**：写入工具返回 `[OK] queued` 而非真实结果。LLM 不需要知道写入是否成功——如果失败记日志即可。读操作正常返回。

### 6.3 可用工具

| Tool Name | category | 参数 | 说明 |
|-----------|----------|------|------|
| `send` | terminal | text, image, face, at, reply_to, forward | 发送消息到用户 |
| `self_note` | write | action (add/replace), content | 管理私人印象。`add` 追加到现有笔记末尾，`replace` 全文覆盖。未调用时笔记不变 |
| `memory_save` | write | content | 写入长期记忆 DB（category="memory"），供 `memory_search` 检索 |
| `memory_search` | read | query（关键词）, limit（默认 5） | FTS5 全文检索长期记忆，返回匹配记录 |
| `http_api_call` | external | method, url, headers, body | HTTP 请求，返回 `resp.text[:4000]` |
| `config_manager` | self-modify | action (get/set/list/reload), key, value | 热读写配置 |

### 6.4 `self_note` Tool 定义

```python
SELF_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "replace"],
            "description": "add: 追加到现有笔记末尾。replace: 全文覆盖。",
        },
        "content": {
            "type": "string",
            "description": "要添加或覆盖的文本。建议 ≤1000 tokens。",
        },
    },
    "required": ["action", "content"],
}
```

**行为**：
- `add` → 读当前 self_note → 拼接 `existing + "\n" + content` → 写入
- `replace` → 直接覆盖为 `content`
- 未调用 → pipeline 不修改 self_note（注入旧内容）
- 每个 `group_key` 只有**一条**活跃 self_note（单行 UPSERT）
- 写入时不做硬截断（弹性空间）；仅注入时超出 `target × max_multiplier` 才保护性截断尾部

**Context 注入格式**：

```
[私人印象 — current: 450 / target: 1000 tokens]
李明，后端开发，养猫叫皮蛋。
最近在关注天气API，上周提过项目延期。
对简洁回复有偏好。
[/私人印象]
```

**System Prompt 新增指令**：

```
你拥有一个 [私人印象] 空间用于维护对用户的私人印象和关键信息。
[私人印象] 标签标注了当前长度与目标上限。
请保持内容精炼，优先保留长期价值高的信息。
使用 self_note(add) 追加新信息，使用 self_note(replace) 重新整理。
如果当前已接近或超出目标长度，请在下一轮对话中主动用 replace 模式精简。
```

### 6.5 `memory_save` / `memory_search` 说明

**职责分离**：

```
self_note      → 私人印象（单条全文，连贯文本，注入 context）
memory_save    → 离散事实（一条一个事实，写入长期记忆 DB）
memory_search  → 关键词检索（FTS5，从长期记忆 DB 拉取）
```

三者不重叠。`memory_save` 只管碎片化事实，交由 `memory_search` 按需检索。`self_note` 是连贯的全文印象，始终注入 context。

两个 tool 共享同一张 `messages` 表，通过 `category` 字段区分（`"self_note"` vs `"memory"`）。

---

## 7. 数据库改动 / Database Schema

### 7.1 新建 `summaries` 表

```sql
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key   TEXT    NOT NULL,
    seq         INTEGER NOT NULL,         -- 单调递增，跨 group_key 唯一排序
    source      TEXT    NOT NULL,         -- "user" | "bot"
    summary     TEXT    NOT NULL,         -- 固定摘要文本，≤100 tokens
    created_at  REAL    NOT NULL DEFAULT (julianday('now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_group
    ON summaries(group_key, seq);
```

### 7.2 `messages` 表新增字段

```sql
ALTER TABLE messages ADD COLUMN category TEXT NOT NULL DEFAULT 'text';
```

`category` 枚举值：

| 值 | 含义 | 写入者 |
|----|------|--------|
| `"text"` | 原始对话消息 | pipeline（每次对话） |
| `"image"` | 图片消息 | pipeline（每次图片） |
| `"mixed"` | 混合消息 | pipeline |
| `"memory"` | LLM 主动保存的事实 | `memory_save` tool |
| `"self_note"`| 模型私人笔记 | `memory_save` tool (category="self_note") |

### 7.3 FTS5 全文索引

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, group_key, category,
    content=messages
);
```

`aiosqlite` 自带 FTS5 支持，无需额外依赖。

### 7.4 `MessageStore` 新增方法

```python
class MessageStore:
    # 已有方法: initialize, save, save_media, get_messages,
    #           get_context_for_group, count, close

    # 新增: summaries CRUD
    async def add_summary(self, group_key: str, source: str, summary: str) -> int
    async def get_summaries(self, group_key: str, limit: int = 180) -> list[dict]
    async def trim_summaries(self, group_key: str, keep: int = 180) -> int

    # 新增: self_note 查询
    async def get_current_self_note(self, group_key: str) -> StoredMessage | None
    async def upsert_self_note(self, group_key: str, content: str) -> None

    # 新增: FTS5 检索
    async def search_memory(self, group_key: str, query: str, limit: int = 5) -> list[StoredMessage]
```

---

## 8. 配置新增字段 / Config

```yaml
context:
  window_max_tokens: 100000    # 工作记忆触发回收的 token 上限
  window_min_tokens: 50000     # 回收后工作记忆保留的 token 目标值
  summaries_max_count: 180     # 摘要表触发回收的条目上限
  summaries_min_count: 90      # 回收后摘要表保留的条目目标值
  debounce_timeout: 1.5        # 防抖窗口（秒）

memory:
  archive_threshold_tokens: 100      # 单条消息超过此值触发归档摘要
  self_note_target_tokens: 1000      # self_note 目标长度
  self_note_max_multiplier: 2.0      # 注入保护截断倍数（target × multiplier 时截断）

model:
  summarizer:                    # 摘要和私思用模型（可选，不配置则走主模型）
    provider: deepseek
    model: deepseek-chat
    base_url: https://api.deepseek.com/v1
    temperature: 0.3
    # api_key 默认复用主 model.api_key
```

---

## 9. 约束条件 / Constraints

1. **摘要文本是固定文本**，写入后不重生成，保证 context 前缀稳定性以利用 API 缓存命中。
2. **工作记忆和摘要均采用增长式 + 折半回收**：超出 max 时批量回收至 min。新内容追加到末尾，不修改已有前缀。
3. **self_note 单条全文注入**，≤1000 tokens。附带长度元数据（current/target）供 LLM 自主管理。超出 `target × max_multiplier` 时保护性截断。LLM 通过 `self_note` tool 编辑（add/replace），system prompt 提醒长度控制。
4. **防抖仅对 TEXT 消息生效**。IMAGE/MEDIA 立即 dispatch 不等待。
5. **取消即争用**：`/break` 或新消息到达 → `cancel_user()` → 清理防抖缓冲和 timer。
6. **混合回复的沉默不允许**在冷启动时发生（`session.is_cold()` → 必须回复）。
7. **FTS5 全文检索无向量依赖**，不引入 FAISS / 嵌入模型。
8. **所有 tool error 返回 `[Error: ...]` 字符串**，不抛异常。
9. **副作用后更新**：所有写入操作（`self_note`、`memory_save`、`config_manager set`、归档、回收）延迟到 `finally` 块批量执行，保证 `/break` 取消时无半提交状态。
10. **send 不终止循环**：仅当本轮无其他 tool 时 send 才是终端操作。send + 其他 tool 时继续下一轮。单次循环最多发送 `MAX_SENDS_PER_LOOP` 条消息。

---

## 10. 实现优先级 / Implementation Order

| Step | 内容 | 涉及文件 | 状态 |
|------|------|----------|------|
| 1 | 防抖系统 | `scheduler.py` + `config.py` | 待实现 |
| 2 | 工具循环闭环 | `pipeline.py` | 待实现 |
| 3 | 混合回复（send tool + content） | `pipeline.py` + `tools/send.py` | 待实现 |
| 4 | `memory_search` + `memory_save` tool | `tools/memory.py` + `memory/store.py`（FTS5） | 待实现 |
| 5 | `summaries` 表 + CRUD | `memory/store.py` | 待实现 |
| 6 | 归档逻辑（post-pipeline 摘要生成） | `memory/archiver.py` + `pipeline.py` | 待实现 |
| 7 | 私思注入（self_notes → context） | `pipeline.py` | 待实现 |
| 8 | Config 字段扩展 | `config.py` + `config.example.yaml` | 待实现 |
