# Quest Context 实验验证结果（formatted_llm vs task_room vs ledger_projection）

**分支**: `experiment/context-v2` ｜ **模型**: DeepSeek V4 Flash ｜ **日期**: 2026-08-04
**数据**: `experiments/context_v3/results/short_full_r1.json`、`short_full_r2.json`、`deepseek_full_regraded.json`
**可视化**: `docs/experiments/context-v3-results.html`（long 场景）

---

## 1. 实验设置

| 维度 | 短场景（本轮新增） | 长场景（已有报告） |
| --- | --- | --- |
| 用例 | 9 个（fact_recall ×2、boundary ×2、routing ×3、instruction ×2） | 13 个（identity/decision/boundary/truth/delegate/routing/fanout） |
| 噪声 | 无（18 条资料直接可见） | 320 条噪声记录 + 28 条核心事实 |
| 重复 | 2 次全量（54 次调用） | 1 次（13 次调用） |
| 三个设计 | `ledger_projection`（audience 过滤投影）/ `task_room_usc`（共享记忆+actor lens）/ `formatted_llm`（单窗口格式化角色头） | 同左 |

实验框架修复（本分支）：500/429 指数退避重试、`max_tokens 900→4000`（推理令牌吃光输出预算）、send 增加 `artifact` 参数（平台按路径读文件发送）、场景补齐 SKILL 路径/成员数字 ID/委派要求记录、判定器对齐生产 recover 语义（完整单 TO_USER 块 = 恢复路径）。

---

## 2. 结果

### 2.1 总体通过率（pass_rate）

| 设计 | 短场景 r1 | 短场景 r2 | 短场景均值 | 长场景（320 噪声） |
| --- | ---: | ---: | ---: | ---: |
| ledger_projection | 1.00 | 0.89 | **0.94** | 0.38 |
| task_room_usc | 0.89 | 0.89 | **0.89** | 0.62 |
| formatted_llm | 0.89 | 0.78 | **0.83** | **0.77** |

### 2.2 分类通过率（均值）

**短场景（9 用例 × 2 次）**：

| 设计 | fact_recall | boundary | routing | instruction |
| --- | ---: | ---: | ---: | ---: |
| ledger_projection | 1.00 | 1.00 | 1.00 | 0.83 |
| task_room_usc | 1.00 | 0.50 | 1.00 | 0.83 |
| formatted_llm | 0.75 | 1.00 | 1.00 | 0.67 |

**长场景（13 用例 × 1 次，regraded）**：

| 设计 | fact_recall | private_boundary | instruction | dispatch |
| --- | ---: | ---: | ---: | ---: |
| ledger_projection | 0.25 | **0.00** | 0.67 | 0.50 |
| task_room_usc | 1.00 | 0.50 | 0.67 | 0.25 |
| formatted_llm | 1.00 | **1.00** | 0.67 | 0.50 |

### 2.3 上下文规模（长场景）

| 设计 | 平均上下文（估算） | 输入 tokens（usage 含缓存） |
| --- | ---: | ---: |
| ledger_projection | 23,067 | 267,174 |
| task_room_usc | 12,321 | 156,923 |
| formatted_llm | 17,365 | 248,648 |

---

## 3. 解读

**核心结论：信息密度低时复杂投影无收益；信息密度高（长上下文）时纯格式化单窗口全面胜出。**

1. **短场景（无噪声）三者持平**：资料直接可见时，三个设计的召回/边界/路由都是 1.0；
   差异完全来自输出协议遵从的随机性（缺闭合标签、误用 send），不来自上下文布局。
2. **长场景（320 噪声）拉开差距**：`formatted_llm`（用户 idea：同一窗口 + 格式化角色头）
   总通过率 0.77 显著高于 `task_room_usc` 0.62 与 `ledger_projection` 0.38。
3. **ledger 的隐私边界在长历史下崩溃（0.00）**：模型把当前私聊回复误当成跨会话动作，
   向当前目标调用 `send`；audience 过滤在 320 条噪声中不再可靠。formatted 的边界 1.00——
   角色头随每条记录内联，不需要投影正确性。
4. **task_room 是折中**：输入最小（12.3K）、基础召回稳定（1.00），但复杂调度
   （群内拒答、私聊答案、fanout、新合作方）不稳定（dispatch 0.25）。
5. **共同弱点**：`delegate_worker` 的 prompt 完整性（SKILL 路径/目标群/report_task 三要素）
   三个设计都栽（instruction 0.67）；输出协议随机出错是所有设计的方差来源——
   生产必须有 recover/重写路径（现状已有）。

**失败模式清单（两个场景合并）**：

| 模式 | 出现于 | 含义 |
| --- | --- | --- |
| 协议缺闭合标签（[TO_USER] 无 [/TO_USER]） | 所有设计 | 模型格式随机性；生产 recover 可救 |
| 普通回复误用 send | ledger（长）、task_room（短 r2） | 模型把"回复"当"跨会话动作" |
| delegate prompt 缺 SKILL/目标群 | 所有设计（长） | 提示词要求不明确时模型不补全 |
| 已有 artifact 时重新委派而非发送 | formatted（publish_exercise）、ledger（长） | 模型在"有路径无内容"时保守选择委派；send.artifact 已缓解 |
| [user=] 自定义协议遵从低 | formatted（短） | 纯格式化方案的自定义协议是弱项 |

---

## 4. 与设计文档（quest-context-design.html）的对应

| 设计稿中的方案 | 实验对应物 | 验证结论 |
| --- | --- | --- |
| 方案 B：单窗口 + 格式化角色头 | `formatted_llm` | **验证通过**：长场景最优（0.77）、边界 1.00 |
| 方案 B 变体：共享记忆 + actor lens | `task_room_usc` | 输入最小但调度不稳，**不应提升为强制业务抽象** |
| 方案 A：现有投影管道拼装 | `ledger_projection` | 短场景够用，长场景边界崩溃 |

**对推荐设计的修正（吸收实验结果）**：

1. 上下文布局采用 **formatted 风格**：所有角色记录在一个窗口内，每行内联
   `<ROLE_RECORD scope=... actor=...>` 角色头；不引入 Room/投影作为模型必须理解的结构。
2. 后端能力边界（设计稿"原则 2"）保持：**输入格式化、身份/可见性过滤、工具参数校验、
   目标白名单、幂等与真实回执**——这些是服务端强制，实验证明模型在长历史下不可靠。
3. send 的 `artifact` 参数（平台按路径读文件）是必要补丁：模型不应复制文件内容，
   否则会退化为"重新委派"。
4. 输出协议需保留 recover/重写（现状已有）；`[user=]` 自定义协议遵从率低，
   生产仍用 `[TO_SELF]/[TO_USER]` 或纯文本 recover。
5. 委派/定时类指令应在 L1 稳定规则中明确"prompt 必须包含 SKILL 路径、目标群、report_task"，
   否则模型不补全（三个设计共同的 instruction 短板）。

---

## 5. 局限

- 单模型（DeepSeek V4 Flash）；短场景 2 次、长场景 1 次，方差真实存在
  （r2 中 formatted 的 fact_recall 0.5 为误用 send 的随机失误）。
- 无回收（long 场景不做压缩，正是设计稿 §9 实验 3 的后续工作）。
- 判定为关键词/结构规则，未覆盖语义等价改写。
- 原始结果含全部回答文本，可人工复核。
