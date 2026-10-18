# Mutsumi's SYNC — 架构设计书（人类阅读版）

**版本**: v2.1  
**日期**: 2026-05-19  
**设计**: 基于 Cancellable Pipeline + 自指工具 的图灵完备 Agent 架构

---

## 一、架构全景

```
                          ┌──────────┐
                          │  NapCat  │
                          │ (QQ框架) │
                          └────┬─────┘
                               │ WebSocket 推送消息
                          ┌────▼──────────────────────────────┐
                          │       PipelineScheduler           │
                          │                                    │
                          │  持有全局状态（所有用户共享）:        │
                          │  • Config (配置)                    │
                          │  • ToolRegistry (工具注册表)        │
                          │  • SkillRegistry (技能注册表)       │
                          │  • ScheduleEngine (定时引擎)        │
                          │  • MemoryStore (长期记忆)           │
                          │  • LLMProvider (模型客户端工厂)     │
                          │  • MessageSender (消息发送器)       │
                          │                                    │
                          │  持有每用户状态:                     │
                          │  • MessageWindow (滑动窗口)         │
                          │  • SessionState (会话状态)          │
                          │                                    │
                          │  _tasks: { user_key → Task }       │
                          │         ↑ 管理协程生命周期           │
                          └────┬──────────────────────────────┘
                               │ 注入所有依赖，启动协程
                               │ 新消息到达 → 取消旧Task → 创建新Task
                          ┌────▼──────────────────────────────┐
                          │   pipeline() 无内部状态函数          │
                          │                                    │
                          │  接收: message, msg_type            │
                          │  依赖: config, registry, skills,    │
                          │        llm, sender, window, ...    │
                          │                                    │
                          │  参考流程 (实现者可调整顺序):         │
                          │  分类 → 去重 → 冷启动 → 向量检索   │
                          │  → 构建上下文 → LLM多轮工具调用     │
                          │  → 发送回复 → 更新窗口              │
                          │                                    │
                          │  ★ 在任何 await 点可被 Task.cancel │
                          │    中断，LLM HTTP 连接随之断开      │
                          └────────────────────────────────────┘
```

### 设计原则

| 原则 | 含义 |
|------|------|
| **单函数核** | 消息处理全部逻辑在 `pipeline()` 一个函数内，AI Agent 可一次性理解 |
| **参数注入** | Pipeline 不持有任何状态，所有依赖通过参数显式传入 |
| **协程即状态机** | 每个用户一个 `asyncio.Task`，新消息 → 取消旧 → 创建新 |
| **版本号驱动自指** | 工具修改全局状态时递增版本号，Pipeline 比较版本号决定是否重快照 |
| **全局共享语义** | Config、ToolRegistry、Skills 是全局单例，任何用户的修改影响所有用户 |
| **技能 = 数据** | Skill 是声明式数据，加载时不执行任意代码 |

---

## 二、自指工具：Agent 管理自己的三个维度

这是本架构的核心创新——Agent 通过工具调用**修改自身的配置、扩展自身的能力、推迟自己的执行**。

### 自指循环示意

```
 ┌────────────────────────────────────────────────────┐
 │                     agent                           │
 │                                                     │
 │  "我应该加载 reminder 技能"                          │
 │         │                                           │
 │         ▼                                           │
 │  ┌─────────────┐     ┌─────────────┐               │
 │  │skill_manager│────▶│SkillRegistry│               │
 │  │   (tool)    │     │ .load()     │               │
 │  └─────────────┘     └──────┬──────┘               │
 │                             │                       │
 │                    ┌────────▼────────┐              │
 │                    │  ToolRegistry   │              │
 │                    │  新增 3 个工具   │              │
 │                    │  version += 1   │    ← 版本号 │
 │                    └────────┬────────┘              │
 │                             │                       │
 │  pipeline 比较版本号:       │                       │
 │  registry.version != my_version                     │
 │  → 重新快照工具列表         │                       │
 │                             │                       │
 │  "我现在可以用 set_reminder 了"                     │
 │         │                                           │
 │         ▼                                           │
 │  ┌──────────────┐                                   │
 │  │set_reminder  │  ← 刚刚加载的工具                  │
 │  └──────────────┘                                   │
 │                                                     │
 └────────────────────────────────────────────────────┘
```

### 六种自指原语

```
         ┌──────────────────┐
         │   World Tools     │  外部世界
         │   http_api_call   │  API调用、文件系统
         └────────┬─────────┘
                  │
    ┌─────────────┼──────────────┐
    │             │              │
┌───▼────┐  ┌────▼─────┐  ┌─────▼────┐
│  Self  │  │  Extend  │  │  Future  │
│ Modify │  │   Self   │  │   Self   │
│        │  │          │  │          │
│config  │  │ skill    │  │scheduler │
│manager │  │ manager  │  │          │
└────────┘  └──────────┘  └──────────┘
    │             │              │
    ▼             ▼              ▼
 修改配置      加载技能       注册定时
 热生效        工具热插拔     未来执行
```

---

## 三、核心机制详解

### 3.1 取消机制

```
用户连续发送两条消息（间隔 1 秒）

时间线:
  0.0s  消息A到达 → Scheduler 创建 Task_A → pipeline(msg=A)
  0.3s  pipeline 执行中: await llm.chat(...)
        ↑ HTTP 请求已发出，等待 DeepSeek 响应
  1.0s  消息B到达 → Scheduler:
        ① task_A.cancel()           ← 取消旧任务
        ② CancelledError 在 llm.chat() 处抛出
        ③ httpx 底层断开 HTTP 连接  ← DeepSeek 停止生成
        ④ pipeline 清理，Task_A 结束
        ⑤ 创建 Task_B → pipeline(msg=B, window=旧窗口, session=旧会话)
        ↑ 窗口和会话由 Scheduler 持有，不随 Task 销毁

注意:
  API 断开连接后，DeepSeek/OpenAI 是否计费取决于具体提供商策略。
  Task.cancel 保证的是客户端行为（连接断开），不保证服务端计费行为。
  LLM API 返回 429/503 时应有退避策略，此为实现细节。

关键设计: 状态在 Scheduler，协程只是状态上的操作。取消操作不影响状态。
```

### 3.2 版本号驱动的自指（替代脏标记）

```
为什么不用布尔脏标记？
  布尔值在多个 Pipeline 并发时存在竞态:
    Pipeline A: 工具执行 → registry.dirty = True
    Pipeline B: registry.list() → dirty = False (重置!)
    Pipeline A: 检查 registry.dirty → False → 丢失变更

版本号方案:
  每个可变状态维护一个单调递增的版本号。
  Pipeline 在工具循环开始时记录当前版本号。
  每次工具执行后比较: 版本号变了 → 重新快照。
  版本号只增不减，不存在跨 Pipeline 互相覆盖。

pipeline 的工具循环:

  tools, my_version = registry.snapshot()   # ① 获快照+版本号
  llm = llm_factory(config)

  for step in range(max):

      response = await llm.chat(messages, tools=tools)

      for each tool_call:
          result = registry.execute(tool_call)

          # ② 检查全局状态是否被修改（版本号比较）
          if registry.version != my_version:
              tools, my_version = registry.snapshot()   # 工具列表变了
          if config.dirty:
              llm = llm_factory(config); config.dirty = False  # 模型参数变了
          if skills.dirty:
              messages[0] = build_prompt(); skills.dirty = False  # 系统提示变了

          messages.append(ToolMessage(result))

      if no tool_calls:
          break

为什么用版本计数器而非 bool？
  bool 竞态: 两个 Pipeline 共享 bool → A 设置，B 重置 → A 丢失变更
  版本计数器: Pipeline 记住自己的版本号，只和 registry.version 比较
  实现简单，无需锁，线程/协程安全。

为什么 config 和 skills 仍用 bool？
  因为 config.set() 和 skills.load() 是低频操作，不存在"A 的设置被 B 误清零"的场景。
  如果将来需要更精确的变更追踪（如"只重建被修改的部分"），可统一为版本号。
```

### 3.3 定时任务 = 合成消息

```
用户: "每天早上八点提醒我吃药"

Pipeline Step 1:
  LLM → scheduler({"action": "register", "schedule": "0 8 * * *",
                     "prompt": "提醒用户 xxx 吃药"})

ScheduleEngine:
  ① 存储任务到持久化存储
  ② 启动 asyncio 定时器

次日 8:00:
  ScheduleEngine._on_trigger(task):
    # ★ 创建合成事件，进入标准 Pipeline
    synthetic = SyntheticEvent(
        user_id=task.user_id,
        raw_message=f"[SCHEDULED:{task.id}] {task.prompt}"
    )
    await scheduler.dispatch(synthetic)
    # ↑ 和用户消息走完全相同的处理路径

Pipeline (处理合成消息):
  LLM 收到 "[SCHEDULED:xxx] 提醒用户 xxx 吃药"
  LLM → sender.send(peer, "该吃药了")
  LLM → scheduler({"action": "cancel", "id": task.id})  # 一次性任务自我注销

优雅之处:
  定时任务不需要特殊的"回调函数"。它只是一条延迟到达的消息。
  Pipeline 不知道、也不需要知道消息来自用户还是定时器。
```

---

## 四、技能系统

### 4.1 核心约束

```
技能 = 数据声明。技能加载 = 注册工具 + 注入 system_prompt 片段。
技能加载时不得执行任意代码。

v1 建议实现: Python 模块
  # skills/reminder.py
  def register(registry: ToolRegistry) -> None:
      registry.register(Tool(
          name="set_reminder",
          description="设置一个定时提醒",
          handler=create_scheduler_handler("register"),
          ...
      ))

  def system_prompt() -> str:
      return "你可以使用提醒工具..."
```

YAML/JSON 声明式是未来可选方案，不作为架构强制要求。具体加载格式由实现者决定。

### 4.2 常见 Handler 路由（实现参考，非架构规定）

| 类型 | 用途 | 备注 |
|------|------|------|
| 内置函数 | 映射到 Python 函数 | config_manager, system |
| HTTP 调用 | 声明式或编程式 HTTP 请求 | 天气查询、翻译 |
| 调度操作 | 定时任务注册/管理 | 提醒、定期汇总 |
| MCP 代理 | 转发到外部 MCP Server | 为未来扩展预留 |

### 4.3 加载流程

```
skill_manager("load", "reminder")
  │
  ├─ SkillRegistry.load("reminder")
  │    ├─ 读取技能定义（Python 模块 或 YAML 文件）
  │    ├─ 构建 Tool 对象列表
  │    ├─ ToolRegistry.register_all(tools)  → registry.version += 1
  │    └─ 缓存 system_prompt 片段           → skills.dirty = True
  │
  └─ Pipeline 检测变更 → 重新快照工具列表和 SystemMessage
     → 下一轮 LLM 调用自动使用新工具
```

---

## 五、工具注册表（核心数据结构）

```python
class ToolRegistry:
    """
    管理所有可用工具。支持运行时热增删。

    使用版本号而非 bool 标记，避免多 Pipeline 并发竞态。
    """
    _tools: dict[str, Tool]
    version: int = 0     # 单调递增，每次注册/删除 +1

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        self.version += 1

    def remove(self, name: str):
        del self._tools[name]
        self.version += 1

    def snapshot(self) -> tuple[list[Tool], int]:
        """返回 (当前工具列表, 当前版本号)。版本号不重置。"""
        return list(self._tools.values()), self.version

    async def execute(self, name: str, args: dict) -> str:
        tool = self._tools[name]
        return await tool.handler(args)
```

---

## 六、Scheduler 调度器（核心协调器）

```
PipelineScheduler 职责:

  1. 持有全局单例依赖 (config, registry, skills, schedule, llm, sender, store)
  2. 持有每用户状态 (window, session, task)
  3. 接收 WS 事件 → 取消旧 Task → 创建新 Task
  4. 接收定时触发 → 构造合成事件 → 创建 Task
  5. 构造 PipelineDeps → 注入 pipeline()

接收两种消息源:
  ┌──────────┐       ┌───────────────┐
  │ Receiver │       │ScheduleEngine │
  │ (WS事件) │       │ (定时触发)     │
  └────┬─────┘       └───────┬───────┘
       │                     │
       ▼                     ▼
  MessageEvent        ScheduledTask → SyntheticEvent
       │                     │
       └──────────┬──────────┘
                  ▼
         dispatch(event)
                  │
           _make_key(event)
           cancel old task
           create new task
                  │
                  ▼
         asyncio.create_task(
             pipeline(msg, *, deps)
         )
```

---

## 七、代码组织（约束）

```
代码按职责分层，具体文件命名和拆分由实现者决定:

  message/      — I/O 层：WebSocket 接收、HTTP 发送、消息分类
  tools/        — 工具实现（纯函数，接收 (args, *, deps)）
  memory/       — 状态存储：滑动窗口、长期记忆、向量索引
  schedule/     — 定时引擎
  provider/     — LLM 抽象
```

---

## 八、关键设计决策清单

| 决策 | 内容 | 理由 |
|------|------|------|
| Pipeline 是单函数 | 所有处理逻辑在一个函数 | AI Agent 可一次性理解全部流程 |
| 参数注入 | Pipeline 不持有状态 | 可测试、可替换依赖、边界清晰 |
| 全局状态共享 | Config/Tools/Skills 所有用户共用 | 单主人 QQ Bot，无需多租户隔离 |
| Task.cancel 打断 | 每个用户一个 Task，新消息取消旧 | asyncio 原生机制，零额外复杂度 |
| 合成消息 | 定时任务 = 伪造一条用户消息 | 消除特殊回调路径 |
| 版本号自指 | 用单调计数器追踪全局状态变更 | 避免布尔脏标记的跨 Pipeline 竞态 |
| 技能 = 数据 | 加载技能不执行任意代码 | 安全、可审计、Agent 可自行生成 |

---

## 九、安全说明

> 具体安全策略（如技能来源信任模型、参数注入检查、API 速率限制）
> 由实现者根据实际部署场景决定。架构层仅约束：技能加载不执行任意代码。
> 对于单主人个人 Bot，复杂的多用户信任模型在 v1 阶段是过度工程。
