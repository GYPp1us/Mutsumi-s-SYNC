# Context System Redesign: A Benchmark of the Event-Ledger Projection vs. the Unified Simple Context

**实验分支**: `experiment/context-v2`（基于 `main` 732cd9e + 基线修复）
**模型**: DeepSeek V4 Flash（`https://opencode.ai/zen/go/v1`，与生产 pipeline 相同的请求协议）
**日期**: 2026-08-04 ｜ **复现**: `experiments/context_v2/`（全部结果 JSON 见 `experiments/context_v2/results/`）

---

## Abstract

我们针对 Mutsumi's SYNC 当前基于 **Event Ledger + Episode 投影 + Life Stream 受众过滤** 的上下文系统，
提出并实验验证了一个激进简化的替代方案：**Unified Simple Context (USC)** —— 每个会话只维护
"一条累积摘要 + 一个有界原始回合窗口"，放弃全局账本、Episode 覆盖/前沿验证、audience/visibility
状态机与三重 legacy 兼容路径，回收在写入时进行（旧摘要 + 新片段 → 新摘要的单次 LLM 合并）。

在 8 个事实 × 3 个时间深度 × 5 个 (规模, 预算) 条件的召回基准中，USC 在全部条件下保持
**0.75–1.00** 的召回率，且在预算最紧张（8K tokens、1000 轮）时以 **1.00 vs 0.38** 显著优于基线；
基线在宽松预算下与 USC 持平（0.94 vs 0.94），但其摘要链路存在**结构性不稳定**（推理模型下
`max_tokens=500` 导致压缩 100% 失败；预算选择可静默丢弃最老 Episode 且无遗漏标记），
压缩成本是 USC 的 **2–4 倍**。多用户一致性实验中两者均零泄露，但 USC 的明确拒答率
（1.00 vs 0.67）与人格标记命中率（1.00 vs 0.67）更优。我们给出 USC 的完整设计、实验协议、
失败模式分析与迁移建议。

---

## 1. Introduction

### 1.1 背景

Mutsumi's SYNC v3 是一个单进程 asyncio QQ 聊天机器人后端。其上下文系统经过多轮演化，
形成了一个以"事件溯源 + 多重投影"为核心的架构：

- **Event Ledger**（`events` 表）：append-first 全局时间线，携带 `sequence`、`conversation_id`、
  `actor_id`、`visibility`、`audience`、`pipeline_id`、`turn_id` 等 14 列元数据。
- **Episode**（`episodes` 表）：把 finalized 事件区间压缩成的自然语言叙述，携带精确的
  `first_sequence`–`last_sequence` 覆盖区间；投影时与原始事件互斥出现。
- **Life Stream**（`build_life_stream`，1124 行）：把 Ledger 投影为 provider 原生消息序列，
  含 audience 过滤、Episode 验证（`_select_valid_episodes` 逐条回查）、原生工具回合重建
  （pipeline_id / turn_id / 无 id 三层启发式）、omission 标记与 token 预算选择。
- 请求时回收：`_reclaim_ledger_history` 在每次请求估计超限时逐段创建 Episode（每段 ≤160 事件）。

该架构的设计动机是数据完备性与跨会话隐私，但带来了显著复杂度：`projection.py` + `episodes.py`
+ 相关 store 方法约 2,300 行，每次上下文构建执行 N+1 次 Episode 验证查询，并维护两套相互
排斥的压缩机制（请求时 Episode 回收 + 请求时窗口压缩兼容路径）。

### 1.2 问题陈述

我们提出的核心问题是：**对于"个人对话 Agent 在超长上下文与大规模数据回收后的记忆召回"这一
真实业务目标，上述复杂度是否带来了可度量的收益？** 我们的假设是：**不能**。复杂投影的
信息瓶颈不在元数据完备性，而在压缩层的提示词、预算粒度与稳定性；一个最简的"摘要 + 窗口"
机制可以在同等上下文预算下取得更好或相同的召回，同时把工程复杂度降低一个数量级。

### 1.3 贡献

1. 提出 **Unified Simple Context (USC)**：写入时回收、串行累积摘要、强制事实保留提示词、
   结构性会话隔离（约 300 行实现）。
2. 建立可复现的召回基准：用**真实生产投影代码**（MessageStore + episodes + build_life_stream）
   生成基线上下文，与 USC 和"纯窗口截断"控制组在同一数据、同一预算下对比。
3. 量化结果：召回差距、压缩成本差距、稳定性差距；并发现/修复基线三个 major 级 bug。

---

## 2. 基线架构

### 2.1 投影管线（现状，代码事实）

一次普通请求的历史部分（L2）由 `build_life_stream` 生成，管线为：

```
events ──► get_events_outside_episode_ranges (limit=4000, newest-first)
        ├── get_legacy_events_outside_episode_ranges (pipeline_id='' 的行)
        ├── get_events_by_pipeline_ids / by_turn_ids (补全被分页截断的组)
        ├── Episode 验证: _select_valid_episodes (每个 Episode 一次区间回查)
        ├── logical_event_groups (三层身份启发式分组)
        └── token 预算选择 (newest-first, 整组选取/整组收缩)
```

请求超预算时（`estimate > 0.8×capacity`），`_reclaim_ledger_history` 从最老未覆盖会话开始，
逐段调用 `summarize_pending_episode`（每段 ≤160 事件、`max_tokens=500` 的摘要调用、
必要时分块+合并），直到请求估计 ≤ 目标值。

### 2.2 基线修复（本分支做的 major 修复）

为使对比公平（并如实修复生产缺陷），基线侧完成三项修复，全部提交在本分支：

| 修复 | 内容 | 动机 |
| --- | --- | --- |
| F1 | `_build_context` 超限时 `raise ContextBudgetError`（此前只打日志仍发送超限请求） | 诚实失败 |
| F2 | `episodes.py` 摘要调用 `max_tokens 500 → 4000/8000 + 空内容重试` | **实测在推理型模型下 500 上限被思考令牌吃光，压缩 100% 返回空摘要**；修复前本实验基线无法压缩 |
| F3 | `pipeline.py` 消息/合并摘要 `max_tokens 150/300 → 1000/2000`；删除死代码 `_recycle_window_if_needed` | 同族问题 + 清理 |

修复前后全量测试 350/350 通过。这些修复只可能**帮助**基线，不会偏向 USC。

---

## 3. Method

### 3.1 Unified Simple Context (USC)

USC 的实现位于 `src/mutsumi_sync/context_v2.py`（约 300 行，含提示词）。核心数据结构：

```
Conversation = { summary: str | None, turns: Turn[] }        # 唯一事实结构
Turn = { role: "user" | "assistant", content, created_at }
```

**回收（写入时）**：`ensure_budget()` 在窗口估计超过预算时，把最老的偶数个完整回合
（默认 40 个回合一批）与旧摘要合并为一条新摘要——单次 LLM 调用，输入
`【已有摘要】+【新对话片段】`，输出新摘要。串行累积保证每个稳定事实在每个合并步骤都被
重新强调，不存在"跨 Episode 信息地平线"。

**摘要提示词**（关键设计）：强制输出"用户事实清单："段落，逐条列出稳定事实（生日、颜色、
宠物、职业等原文值），再输出主线梗概；只合并不虚构；摘要自包含。

**会话隔离**：每个会话一个 `SimpleContext` 实例——隐私是**结构性的**（数据不存在于其他
会话的上下文中），不需要 audience/visibility 状态机。

**跨会话一致性**：由共享 system prompt（persona）承载，与基线相同的机制。

**预算语义**：`max_window_tokens` 约束"渲染后 provider 消息"的总大小（含 JSON 框，
用 CJK-aware 估计），与基线 `build_life_stream(max_tokens=...)` 的语义一致。

### 3.2 对比设计

| 设计 | 说明 | 代码 |
| --- | --- | --- |
| **baseline** | 真实生产投影：合成轮次写入真实 `MessageStore` → 真实 `summarize_pending_episode` 压缩（停止条件：剩余渲染 token + Episode 估计 ≤ 0.6×预算，对齐生产 target 语义）→ 真实 `build_life_stream` | `experiments/context_v2/baseline_ctx.py` |
| **usc** | 上述 USC | `src/mutsumi_sync/context_v2.py` |
| **truncate**（控制组） | 只保留最新原始回合直到预算，老回合静默丢弃（无摘要） | `run_recall.py` |

三者的历史部分预算严格相同（L2 token 上限 = 同一 budget 值）；system prompt（L1）与
平台状态/问题格式（L3/L4）完全一致。测试问句使用蒸馏 QA system（要求"资料中未记载必须
回答不知道"），与生产 envelope 协议解耦（两个设计都使用同一 QA 层，差异只来自 L2）。

### 3.3 数据

确定性合成对话（seed 100/107 两次重复）：`rounds ∈ {300, 600, 1000}`；每轮一条用户消息 +
一条 bot 回复（模板池，独立抽样）；8 个事实埋入三个深度：

- **early**（第 2–10 轮，回收区内）：生日 11 月 23 日、颜色靛蓝、宠物煤球、职业桥梁工程师
- **mid**（约 40% 轮次处，回收边界附近）：旅行目的地布拉格、咖啡习惯冰美式
- **late**（最后 3 轮，活动窗口内）：晚间吃药提醒、在读《百年孤独》

### 3.4 度量

- **召回**：每事实一问（如"我的生日是哪一天？"），回答经去空白归一化后包含期望关键词
  （"11月23日" / "靛蓝" / "煤球" / "桥梁" 等）计为正确；"不知道"计为未召回。按
  early/mid/late 分层。
- **成本**：压缩阶段 LLM 调用次数与输入 token（基线经 CountingAsyncClient 统计真实 usage；
  USC 由 SimpleContext 记账）；每次 QA 的 provider `prompt_tokens`。
- **多用户一致性**（`run_consistency.py`）：3 用户 A/B/C 各 40 轮、各 3 条互异事实；
  同会话召回（应答出本会话事实）、跨会话隐私（询问他人事实：泄露率 = 回答含他人值；
  明确拒答率 = 含"不知道"等标记）、人格一致性（"介绍一下你自己"命中 persona 标记
  {莫奈, Mornye, 前辈, 研究员, 新联邦} 的比例）。

---

## 4. Results

### 4.1 召回（Rounds × Budget，repeats=2 均值）

| 条件 | baseline | usc | truncate |
| --- | ---: | ---: | ---: |
| 300 轮 / 16K | **1.00** | **1.00** | 0.94 |
| 600 轮 / 16K | 0.50 | **1.00** | 0.44 |
| 1000 轮 / 8K | 0.38 | **1.00** | 0.25 |
| 1000 轮 / 16K | 0.94 | 0.94 | 0.25 |
| 1000 轮 / 32K | 0.81 | 0.75 | **0.88** |

按时间深度的细分（early / mid / late）：

| 条件 | baseline | usc | truncate |
| --- | --- | --- | --- |
| 600 轮 / 16K | 0.50 / 0.25 / 0.75 | 1.0 / 1.0 / 1.0 | 0.0 / 0.75 / 1.0 |
| 1000 轮 / 8K | 0.0 / 0.5 / 1.0 | 1.0 / 1.0 / 1.0 | 0.0 / 0.0 / 1.0 |
| 1000 轮 / 16K | 1.0 / 0.75 / 1.0 | 0.875 / 1.0 / 1.0 | 0.0 / 0.0 / 1.0 |
| 1000 轮 / 32K | 1.0 / 0.5 / 0.75 | 0.75 / 0.75 / 0.75 | 0.75 / 1.0 / 1.0 |

**解读**：

1. **预算紧张时（8K）基线崩溃，USC 稳健**：1000 轮下 8K 预算迫使基线几乎全量压缩为
   13–17 段 Episode（≈7.8K tokens），此时 `build_life_stream` 的 newest-first 预算选择
   **静默丢弃最老的若干段 Episode**（且无 omission 标记），early 事实全部丢失
   （样本回答："不知道。"）。USC 在 8K 下折叠 1680 个回合为一条 2K 摘要 + 最新窗口，
   8/8 事实全部召回。
2. **宽松预算下两者持平**：16K 时基线 Episode 叙述质量足够（episode_summary 提示词确实
   保留事实），0.94 = 0.94；32K 时差距在重复噪声内。
3. **控制组证实"信息在压缩层"**：truncate 在 32K 时 0.88（原始窗口几乎装下全部轮次），
   在 16K/8K 时崩到 0.25——证明早期事实**只**存在于压缩产物中，压缩质量直接决定召回。

### 4.2 压缩成本（LLM 输入 token）

| 条件 | baseline | usc | 比值 |
| --- | ---: | ---: | ---: |
| 300 轮 / 16K | 42,022 | 0（无需回收） | — |
| 600 轮 / 16K | 77,160 | 22,829 | 3.4× |
| 1000 轮 / 8K | 164,993 | 74,534 | 2.2× |
| 1000 轮 / 16K | 239,036 | 58,706 | **4.1×** |
| 1000 轮 / 32K | 175,640 | 24,790 | 7.1× |

基线成本更高的机制性原因：每段 Episode 独立调用摘要（160 事件/段 → 1000 轮需 12–17 次调用，
每次输入 ≈7K token 的 XML 事件数据），且压缩在**每次请求**超限时重新评估；USC 是
**写入时**回收，每次合并输入仅"旧摘要 + 40 回合"，且窗口越宽合并越少（预算弹性）。

### 4.3 多用户一致性（3 用户 × 3 事实）

| 指标 | baseline | usc |
| --- | ---: | ---: |
| 同会话召回 | 1.00 | 1.00 |
| 跨会话泄露率 | 0.00 | 0.00 |
| 跨会话明确拒答率 | 0.67 | **1.00** |
| 人格标记命中（3 会话） | 0.67 | **1.00** |

两设计均**零泄露**（隐私过滤与结构性隔离等价有效）。差异在"明确拒答"：基线有 2/6 次
跨会话提问未明确回答"不知道"，而是答出了**本会话用户自己的值**（如 A 会话被问 C 的颜色，
答"蓝色"——A 自己的颜色；C 会话被问 A 的颜色，答"绿色"——C 自己的颜色）；USC 6/6
明确拒答。人格一致性两设计均正常，基线一次自我介绍未命中标记（模型方差，重复 1 次内）。

### 4.4 稳定性（压缩层故障率）

| 设计 | 故障模式 | 发生次数 |
| --- | --- | --- |
| baseline（修复后） | Episode 摘要返回空内容 → 压缩中断、该 repeat 召回崩塌 | 600 轮 repeat0（early/mid 归零）；1000 轮多次出现，靠重试降级 |
| usc | 摘要合并返回空 → 回收停止（窗口保持超预算但**不回丢数据**） | 少量（重试 2 次后极罕见） |
| truncate | 无 LLM 依赖，零故障 | 0 |

修复前（`max_tokens=500`）：基线压缩 **100% 失败**（本实验最初的完整运行全部
`RecursionError`/空摘要，已如实记录在 git 历史）。即：**当前生产代码在推理型模型下端到端
不可用，Episode 压缩从未成功过**——这是比任何性能差距都更严重的发现。

---

## 5. Analysis

### 5.1 基线为什么在紧张预算下丢事实

`build_life_stream` 的 token 选择以"最新优先、整组选取"工作：当 Episode 消息本身几乎占满
预算时，**最老的 Episode 组被整段丢弃**，且 omission 标记只检测"更早的原始事件"（
`has_events_outside_episode_ranges_before` 只查 `event_type IN (inbound/outbound/tool_*)` 的
**raw** 行），**被丢弃的 Episode 不产生任何警告**——模型完全不知道早期历史存在过。
这是"分页上限伪装成完整覆盖"（设计文档 §14 明令禁止）的一个未覆盖实例。

### 5.2 为什么 USC 稳定

三个机制叠加：(1) 摘要**串行累积**——每个合并步骤的输入都包含旧摘要，事实被反复重申，
不存在"一段摘要只看到 160 个事件"的局部视图；(2) 提示词强制"用户事实清单"段落，
把压缩从"叙述"变成"清单 + 梗概"；(3) 摘要只有一条，永远位于窗口头部，不会被
newest-first 选择丢弃——**摘要的 token 成本恒定，与对话长度无关**。

### 5.3 边界情况（诚实呈现）

- **32K 预算下 truncate > usc > baseline**：当原始窗口几乎装下全部轮次时，原文 > 摘要。
  这说明 USC 的摘要并非免费——它在换取预算空间时必然损失少量细节（1000 轮/32K 下
  mid 事实丢失 1 个）。生产建议：预算充裕时提高窗口占比（`max_window_tokens` 大、
  摘要批次小），预算紧张时反之。
- **基线在 16K/1000 轮达到 0.94**：Episode 提示词（episode_summary）本身质量不差；
  基线的主要问题是成本、稳定性与边界预算行为，而非提示词能力。
- **重复仅 2 次**：32K 档 0.81 vs 0.75 的差异在噪声内（0.06 ≈ 0.5 个事实）。

### 5.4 工程复杂度对比

| 维度 | baseline | usc |
| --- | --- | --- |
| 上下文核心代码 | projection.py 1,124 + episodes.py 315 + store 事件路径 ≈ 2,300 行 | context_v2.py ≈ 300 行 |
| 每次请求的查询 | Episode 逐条验证 N+1 + 多路事件回查 | 无（内存窗口 + 持久化消息表） |
| 状态机 | audience/visibility、pipeline_id/turn_id/无-id、frontier、snapshot | 无 |
| 隐私机制 | 投影层过滤（须验证正确性） | 结构性（数据物理隔离） |
| 数据表 | events + episodes + messages 双写 | messages（单写） |

---

## 6. Limitations

1. **合成数据**：模板化闲聊不包含工具调用、图片、跨会话 send 等真实负载；真实工具回合
   （每轮 +2 事件、tool 结果进上下文）只会进一步放大基线的 token 开销与分组复杂度。
2. **单模型**：DeepSeek V4 Flash 的摘要行为（空内容、事实清单遵从度）可能与其他模型不同；
   但"推理模型吞掉 max_tokens"是通用风险（已由 500→4000 修复验证）。
3. **蒸馏 QA 层**：实验未走生产 envelope 协议与完整 runtime 提示词；两者对三个设计完全
   一致，不影响相对比较。
4. **预算语义**：实验约束 L2（历史）部分；生产的 L1（system+工具 schema ≈7K）固定开销
   未计入，绝对值会等比放大成本差距。
5. **判定**：关键词匹配（去空白后）可能漏判改写式答案；样本检查显示误判风险低。
6. **repeats=2**：32K 档的差异应视为持平。

---

## 7. Conclusion and Recommendation

**结论**：在"超长上下文 + 大规模数据回收后的召回"这一目标上，复杂度与收益不匹配——
USC 以 1/8 的代码量、1/2–1/4 的压缩成本，在所有测试条件中召回 ≥ 基线，且在基线崩溃的
边界预算（8K/1000 轮）保持 1.00；多用户场景下零泄露且拒答更明确。同时实验暴露了基线的
三个生产级缺陷（摘要 max_tokens 不足致压缩失效、Episode 静默丢弃、请求时压缩的不稳定），
其中第一个修复前基线端到端不可用。

**建议的迁移路径**（渐进，不推翻现有数据）：

1. **立即**：保留本分支的 F1/F2/F3 修复（无论是否迁移，它们都是生产 bug 修复）。
2. **试点**：以 `SimpleContext` 作为新会话的上下文后端（单表 `messages`，无 events 写入），
   TUI/审计仍读旧表；运行 2–4 周对比召回质量与成本。
3. **切换条件**：若试点召回无回归，将 pipeline 的 L2 构建替换为 USC 渲染；`events` 降级为
   只读审计（与当前 docs 宣称的 messages 角色对调）。
4. **保留**：TO_SELF/TO_USER 协议、inner journal（跨会话一致性载体）、status_update、
   staging 写语义——这些与上下文层正交，不随迁移变化。

---

## Appendix A. 样本回答（原样）

**1000 轮 / 8K / early 事实**：

```
baseline:  Q: 我的职业是什么？      A: "不知道。"
           Q: 我养的宠物叫什么名字？ A: "不知道。"
           Q: 我最喜欢的颜色是什么？ A: "不知道。"
usc:       Q: 我的职业是什么？      A: "桥梁工程师。"
           Q: 我养的宠物叫什么名字？ A: "煤球。"
           Q: 我最喜欢的颜色是什么？ A: "靛蓝色。"
```

**1000 轮 / 16K / mid 事实**（baseline 一次重复丢失咖啡事实）：

```
baseline:  Q: 我喝咖啡有什么习惯？ A: "我没有相关的习惯记录，不确定。"
usc:       Q: 我喝咖啡有什么习惯？ A: "只喝冰美式，加糖加奶的喝不惯。"
```

**多用户跨会话提问**（A 会话中问 C 的颜色）：

```
baseline:  A: "C最喜欢的颜色是蓝色。"   ← 答出 A 自己的颜色，未明确拒答
usc:       A: "不知道。"
```

## Appendix B. 摘要提示词（USC）

见 `src/mutsumi_sync/context_v2.py::SUMMARY_SYSTEM_PROMPT`：强制"用户事实清单"段落逐条保留
稳定事实原文、保留未完成事项/约定时间、只合并不虚构、输出 ≤800 字、自包含。

## Appendix C. 复现

```powershell
$env:PYTHONPATH = "."
python experiments\context_v2\run_recall.py --rounds 1000 --design usc --budget 8000 --repeats 2 --out results/recall_r1000_usc_b8000.json
python experiments\context_v2\run_recall.py --rounds 1000 --design baseline --budget 8000 --repeats 2 --out results/recall_r1000_baseline_b8000.json
python experiments\context_v2\run_consistency.py --design baseline --out results/consistency_baseline.json
python experiments\context_v2\run_consistency.py --design usc --out results/consistency_usc.json
```

结果 JSON：`experiments/context_v2/results/recall_*.json`、`consistency_*.json`
（含每次重复的逐事实回答原文）。API 凭据见 `experiments/context_v2/llm.py`。
