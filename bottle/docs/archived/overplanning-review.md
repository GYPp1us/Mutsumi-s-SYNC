# 评审报告：过度规划分析

## 1. 过度规划的实例

### 【严重】文件布局的具体命名（两文档）

- **位置**: `architecture-for-ai.md §10` + `architecture-for-humans.md §7`
- **内容**: 规定了完整的目录树，包括 `scheduler.py`、`pipeline.py`、`deps.py`、`tools/registry.py`、`tools/config_manager.py`、`skills/handler.py`、`provider/openai_compat.py`（AI 版）或 `provider/llm.py`（人类版）等。
- **问题**:
  1. 两版文档自己就不一致（`openai_compat.py` vs `llm.py`），说明这些名字在写文档时都是随意定的。
  2. 文件命名、拆分粒度、模块边界 **应该是实现者根据实际代码多少做的决策**，而不是架构设计的规定。
  3. 当前代码仅有 94 行的 `bot.py`，在这种规模下规定十几个文件的具体命名，属于严重过度指定。
- **建议**: 删除整个文件布局章节。替换为一条宽松约束：*"代码按职责分层组织在 `message/`、`processor/`、`memory/` 等包中，具体文件结构由实现者自行决定。"*

---

### 【严重】ToolHandler 判别联合体的精确类型

- **位置**: `architecture-for-ai.md §5.2`
- **内容**: 定义了 `BuiltinHandler(fn: Callable)`、`HttpHandler(method, url_template, headers, response_template)`、`SchedulerHandler(action)`、`MCPHandler(server, tool)` 四种精确类型和各自字段。
- **问题**:
  1. 这是在模拟一个类型分派系统。当前阶段（94 行单体代码）需要的是一个路由系统还是一个简单的 callable 列表？
  2. `HttpHandler` 试图用 YAML 声明式描述 HTTP 请求，包括 `response_template`（Jinja2 模板），这本身就是一个微型语言。如果有现成的 `http_api_call` 内置工具，为何还需要声明式 HTTP handler？
  3. `composite` 类型在人类版中出现但 AI 版没有，说明设计还在摇摆。
  4. 这种精确的类型层次限制了实现者的选择——也许更好的做法是 `handler: Callable`，把类型分派延迟到实现阶段。
- **建议**: 将 ToolHandler 约束为 `handler: Callable[[str, dict], Awaitable[str]]`（名字+参数字典 → 结果字符串）。在实现阶段如果发现需要声明式 HTTP 或 MCP，再引入具体类型。

---

### 【严重】Dirty Flag 机制的详细协议

- **位置**: `architecture-for-ai.md §4` + `architecture-for-humans.md §3.2`
- **内容**: 规定每个可变依赖必须有 `_dirty: bool` 标志，`list()` 重置 dirty，`register()` 设置 dirty，pipeline 在工具循环中每步检查三个 dirty 条件。
- **问题**:
  1. 这是"如何"（how）级别的规定。"什么"（what）是：*"工具可以修改自身的环境和能力，修改应在本轮调用的后续步骤中生效。"*
  2. 具体用什么机制（dirty flag、快照、事件、返回值）是实现的决策空间。Dirty flag 只是实现方案之一。
  3. 该方案增加了认知负担：每个 `ToolRegistry`、`Config`、`SkillRegistry` 都要维护 dirty 状态，pipeline 每个工具调用后有 3 个 if 分支。实现者可能选择更简单的方案：每次 LLM 调用前全部重建（性能可接受则更清晰）。
- **建议**: 将 dirty flag 从架构约束降级为"实现建议"或"参考方案"。架构层只约束：*"对全局状态的修改应在同一 pipeline 调用中可见"*。具体机制留给实现者。

---

### 【中等】Config Schema 的完整 YAML 定义

- **位置**: `architecture-for-ai.md §12`
- **内容**: 完整的 YAML schema 定义，包括 `vector.dimension`（默认 1536）、`cache.meme_desc`（路径字符串）、`skills.dir`、`skills.autoload` 等字段及其默认值。
- **问题**:
  1. 配置的默认值、具体字段名、嵌套结构属于实现细节。例如 `vector.dimension: 1536`——为什么是 1536？如果实现者想用不同的 embedding 模型怎么办？
  2. `cache.meme_desc` 是当前功能（表情包缓存）的配置，不属于架构层。
  3. 将配置结构冻结为详尽 schema 使修改配置的成本变高。实现阶段可能需要频繁添加/调整配置项。
- **建议**: 保留配置的高层分类（napcat、model、context、skills 等），但删除默认值、具体字段名和实现相关的配置项。改为：*"Config 至少包含 LLM、NapCat、上下文窗口大小的配置，具体字段由实现定义。"*

---

### 【中等】Skill YAML 声明系统的完整设计

- **位置**: `architecture-for-humans.md §4`
- **内容**: YAML 技能文件规范（含 `version`、`requires` 依赖解析、`system_prompt`、`tools` 列表）、递归加载流程、handler 路由类型表。
- **问题**:
  1. 定义了 `version: "1.0.0"` 语义版本号和 `requires: ["dependency-name"]` 依赖解析。对于一个预期少于 20 个技能的 Bot，版本管理和依赖解析可能是过度工程。
  2. "Handler 不包含代码" 的假设未经验证——已知存在需要业务逻辑的技能（如数据转换、多步条件判断），YAML 声明无法表达。
  3. 当前代码甚至没有 skill 系统——从 0 直接跳到 YAML 声明式 + 依赖解析 + 递归加载，跳跃太大。
- **建议**: v1 技能系统可以用 Python 模块：`skills/reminder.py` 导出 `tools: list[Tool]` 即可。YAML 声明式应该是"未来可选"，而非"架构规定"。

---

### 【中等】安全信任模型

- **位置**: `architecture-for-humans.md §九`
- **内容**: 将技能分为 `builtin/`、`user/`、`generated/` 三个目录，每类有不同信任等级；Agent 生成的技能限制 handler type；首次使用需用户确认。
- **问题**:
  1. 这是单体个人 Bot，谁在使用？如果只有 Bot 主人自己，这套信任模型完全没有必要。
  2. 即使有多用户，信任模型也应该在需求出现时设计，而不是预先架构。
  3. YAGNI 严重违规。
- **建议**: 删除整个 §九。替换为：*"安全考虑由实现者在实现阶段根据实际部署场景决定。"*

---

### 【中等】MCP 兼容性讨论

- **位置**: `architecture-for-humans.md §八`
- **内容**: MCP 在架构中的定位、MCP handler 类型的设计、与本地 Skill 的对比。
- **问题**:
  1. 项目目前不依赖 MCP，也没有计划使用 MCP。整节是未来预测。
  2. 文档自己说 "MCP ... 并非架构的核心"——既然不是核心，为什么要写在架构设计书里？
  3. 如果将来需要 MCP，在 ToolRegistry 中加一个 MCP executor 即可，不需要架构层预留。
- **建议**: 删除 §八。如果需要提及 MCP，写一句注脚：*"架构预留了通过 MCP 扩展的可能性，具体在实现阶段决定。"*

---

### 【中等】ScheduleEngine 多格式支持

- **位置**: `architecture-for-ai.md §5.4`
- **内容**: `schedule` 字段支持 cron 表达式 + `"in:10min"` + `"at:ISO8601"` 三种格式。
- **问题**:
  1. 三种解析器 vs 一种解析器的复杂度差异。Cron 表达式可以覆盖循环和一次性任务（仅未来时间点的 cron 表达式）。
  2. "x分钟后提醒" 是用户自然语言输入 → LLM 解析 → 输出格式的问题。LLM 可以将"10分钟后"翻译成 cron 或 ISO 格式，不需要引擎支持自然语言格式。
- **建议**: 约束放松为：*"ScheduleEngine 至少支持 cron 表达式"*。`in:` 和 `at:` 格式作为实现阶段的性能优化，非架构约束。

---

### 【轻微】MessageWindow 精确方法和签名

- **位置**: `architecture-for-ai.md §5.5`
- **内容**: `max_size: int = 20`、`search(query)` 方法签名。
- **问题**:
  1. `max_size = 20` 是硬编码默认值，应该在 config 中定义。
  2. `search()` 方法的功能边界不清楚——是在窗口内全文搜索还是关键词匹配？留给实现者更合适。
- **建议**: 将 MessageWindow 约束为：*"保存最近 N 条对话，提供上下文构建功能。"* `search()` 作为可选 enhancement。

---

### 【轻微】Pipeline 内部流程的详细步骤顺序

- **位置**: `architecture-for-ai.md §3.1`，步骤 1-7
- **内容**: 精确的步骤顺序：CLASSIFY → DEDUP → COLD START → VECTOR → CONTEXT → TOOL LOOP → CLEANUP。
- **问题**:
  1. COLD START（发送戳一戳）是具体的 UX 决策，不一定是 pipeline 的固定步骤。
  2. VECTOR shortcut（向量直接回复）的实现细节（`top_k=3`、`THRESHOLD`）是调参问题，不是架构问题。
  3. 步骤的严格顺序限制了对 pipeline 进行重组的自由度。
- **建议**: 保留步骤列表作为参考流程，但明确标注"实现者可调整顺序和步骤"。

---

## 2. 合理的抽象/规划

以下决策在合理粒度上停留，没有过度越界：

| 决策 | 文件位置 | 理由 |
|------|---------|------|
| **Pipeline 作为单函数核** | AI §3, 人类 §一 | 只约束"所有逻辑在一个函数内"，不规定具体参数组织和内部实现 |
| **Scheduler 持有全局状态** | AI §7, 人类 §六 | 正确的"什么"——谁拥有状态，不规定具体如何管理 |
| **Cancellation = Task.cancel()** | AI §8, 人类 §3.1 | 使用 asyncio 原生机制，不发明自定义协议 |
| **定时任务 = 合成消息** | 人类 §3.3 | 优雅的概念统一，不规定实现细节 |
| **不依赖 TUI** | AI §1 | 清晰的边界决策，不是技术选型 |
| **ToolRegistry 的概念** | AI §5.1, 人类 §五 | 只约定"工具是注册的 name + handler"，不规定 handler 具体怎么做 |
| **pipeline() 不持有状态** | AI §11 invariant 1-2 | 架构级别的正确约束，不涉及实现 |
| **系统提示词构建** | AI §3.1 step 6 | 抽象级别正确——"构建系统提示"，不规定怎么构建 |
| **Sending = fire-and-forget** | AI §8 | 语义级别的决策，不规定具体实现 |

## 3. 总体评估

### 过度规划等级：**中等偏高（Medium-Severe）**

- 两份文档在 **架构方向级别的决策**（pipeline 模式、合成消息、状态集中管理）上做得很好
- 但在 **实现细节**（文件命名、类型签名、数据结构字段、默认值、安全模型）上过度指定
- 过度规划的比例估计：**约 40% 的内容属于应由实现阶段决定的内容**
- AI 版本和人类版本本身存在不一致（文件命名、handler 类型），暴露了文档规定了自己尚未确定的内容

### 总体的修改建议风格

**"从架构设计书降级为实现指南"**：

1. 保留的核心约束（不应放松）：
   - Pipeline 是单函数，接收所有依赖作为参数
   - Scheduler 持有全局和每用户状态
   - 定时任务是合成消息
   - 取消使用 Task.cancel()
   - Pipeline 不持有可变状态

2. 需要从"规定"降级为"建议"的内容：
   - Dirty flag 机制 → 同一调用中动态变化可见（不限机制）
   - ToolHandler 类型 → Callable 即可，具体类型实现阶段引入
   - YAML Skill 声明 → Python 模块即可，YAML 作为未来选项
   - 文件布局 → 只约束包的边界，不规定文件名
   - Config schema → 只约束必需项，不规定默认值和实现细节

3. 需要删除的内容：
   - §九 安全信任模型 (YAGNI)
   - §八 MCP 兼容性 (未来预测)
   - 精确的文件名和目录树
   - 版本对比表（属于回顾，不在架构设计中）

### 如果按此设计实现，预期的主要风险

1. **实现者被迫遵循未经验证的机制**：Dirty flag 系统、YAML skill 声明、ToolHandler 路由，这些在实现中可能被发现是笨重的或不够用的，但架构文档的权威性会阻碍简化。

2. **过度工程的心理负担**：一个 94 行的单体 Bot，面对 400+ 行的架构文档，开发者可能觉得自己在写一个"系统"而不是一个 Bot，导致无意识的过度设计。

3. **YAGNI 机制浪费实现时间**：安全信任模型、MCP handler、`composite` handler、`in:` 时间格式、Skill 版本管理——这些在 v1 中几乎肯定用不上，但开发者可能会花时间实现它们"以防万一"。

4. **配置僵化**：详细的 Config Schema 使得添加新配置项需要"违反架构设计"，增加实现阶段的摩擦。

5. **类型系统膨胀**：ToolHandler 的 4-5 种类型加上每个的字段，加上 dirty flag 协议，加上 Skill 的字段——这些类型定义本身就需要维护，而它们提供的实际价值在 MVP 阶段接近零。

6. **Agent 自指能力的假设崩塌**：如果 LLM 在实际测试中不善于使用 config_manager/skill_manager 来自我修改（例如总是忘记调用、调用错误、或产生意外状态），整个脏标记系统的复杂度就白费了。这个风险应该在原型中验证，而非在架构文档中"固化"。
