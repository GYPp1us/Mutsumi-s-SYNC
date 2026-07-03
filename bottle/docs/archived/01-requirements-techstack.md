# 需求完成度 & 技术栈契合度评估

**评估日期**: 2026-05-19  
**评估分支**: `main` (HEAD: 67cb628)  
**目标**: Mutsumi's SYNC — QQ LLM 聊天机器人

---

## 一、需求完成度

需求来源：`PROMPT/init.md`（项目章程）、`README.md`、设计文档

| # | 功能需求 | 源 | 状态 | 证据 |
|---|---------|-----|------|------|
| 1 | WebSocket 消息接收 | init.md L28-30 | ✅ 已实现 | `message/receiver.py:112`，含断线重连+指数退避 |
| 2 | HTTP API 消息发送 | init.md L89 | ✅ 已实现 | `message/sender.py:81`，支持私聊/群聊/Poke |
| 3 | 消息分类（短/长文本） | init.md L32-39 | ⚠️ 部分实现 | 分类器完成但 `MEME` 类型永不赋值；短文本未走向量捷径 |
| 4 | **FAISS 向量检索** | init.md L11, 42-44 | ❌ 未实现 | `faiss-cpu` 在 `requirements.txt` 中但零导入；`vector.py` 是 numpy 裸实现，且 `add()`/`search()` 从未被调用 |
| 5 | LLM 模型 Pipeline + Tool Calling | init.md L46-60 | ✅ 已实现 | `processor/pipeline.py:305`，支持多轮 Tool 调用+降级 JSON 解析 |
| 6 | 内置 Tool: config_manager | init.md L57 | ✅ 已实现 | `processor/tools.py:12`，支持 get/set/list/reload，点号路径遍历 |
| 7 | 内置 Tool: http_api_call | init.md L59 | ✅ 已实现 | `processor/tools.py`，ThreadPoolExecutor 兼容异步事件循环 |
| 8 | **多模态输出（CQ码）** | init.md L61-65 | ❌ 未实现 | `sender.py` 仅输出纯文本 `[{"type":"text",...}]`；无 image/at/reply 等 CQ 码 |
| 9 | 滑动窗口上下文 | init.md L67-69 | ✅ 已实现 | `memory/window.py:18`，deque 实现，正确接入 `bot.py` |
| 10 | **PostgreSQL 长期记忆** | init.md L70 | ❌ 未接入 | `memory/postgres.py:39` 类存在，但 `bot.py` 从未导入或实例化 |
| 11 | 消息去重（防抖） | init.md L73-79 | ⚠️ 逻辑缺陷 | `should_reply()` 永远返回 `True`；`schedule_reply()` 从未被调用 |
| 12 | **角色权限管理** | init.md L81-84 | ❌ 未接入 | `AuthManager` 在 `bot.py:42` 实例化，但 `is_admin()` 从未调用 |
| 13 | **表情包缓存** | init.md L38-39, 78 | ❌ 未接入 | `MemeCache` 在 `bot.py:43` 实例化，但 `.get()`/`.set()` 从未调用 |
| 14 | TUI 终端界面 | README L12-15 | ⚠️ 碎片化 | 3 套实现：实际使用 `start_tui.py` 中的 `SimpleREPL`，`repl.py`（prompt_toolkit）和 `app.py`（Textual）均为孤立死代码 |
| 15 | 配置系统 | init.md L92-121 | ✅ 已实现 | `config.py:103`，pydantic 模型+YAML 序列化 |
| 16 | 系统 Prompt 自定义 | init.md L119-125 | ✅ 已实现 | `config.yaml` 中完整系统 Prompt，含 Tool 使用指南 |
| 17 | 冷启动检测 + Poke | bot.py L50-57 | ✅ 已实现 | 超时阈值 300s，自动发送戳一戳 |
| 18 | **`start.py` 简单启动入口** | README, AGENTS.md | ❌ 文件不存在 | 当前分支 `main` 无此文件 |

### 完成度统计

| 类别 | 数量 | 占比 |
|------|------|------|
| 完全实现 | 9 | 50% |
| 部分实现/有缺陷 | 3 | 17% |
| 完全未实现/死代码 | 6 | 33% |

---

## 二、技术栈契合度

### 当前技术栈

| 依赖 | 版本要求 | 实际使用 | 必要性 |
|------|---------|---------|--------|
| `langchain` | >=0.1.0 | 仅 `@tool` 装饰器 (tools.py:1) | 低 — 可用纯函数替代 |
| `langchain-openai` | >=0.0.5 | `ChatOpenAI` 客户端 (pipeline.py:44) | 中 — 核心功能依赖 |
| `langchain_core` | (传递依赖) | `convert_to_openai_function` + 消息类型 | 中 — 与 langchain-openai 联动 |
| `httpx` | >=0.26.0 | sender.py + tools.py + app.py | 高 — 多处使用 |
| `pydantic` | >=2.5.0 | config.py + receiver.py + sender.py + classifier.py | 高 — 贯穿全栈 |
| `pyyaml` | >=6.0.1 | config.py | 高 — 配置加载 |
| `websockets` | >=12.0 | receiver.py | 高 — WebSocket 通信 |
| `psycopg2-binary` | >=2.9.9 | 仅 postgres.py 中懒加载，且类未被使用 | **无 — 死依赖** |
| `faiss-cpu` | >=1.7.4 | **零导入，零使用** | **无 — 死依赖** |
| `python-dotenv` | >=1.0.0 | **零导入，零使用** | **无 — 死依赖** |
| `textual` | >=0.40.0 | app.py + widgets + screens（未使用） | **无 — 死代码依赖** |
| `prompt_toolkit` | >=3.0.0 | repl.py + theme.py（未使用） | **无 — 死代码依赖** |
| `numpy` | **未声明** | vector.py:2 中导入 | **缺失依赖** |

### 关键评估

**1. LangChain 是沉重的薄封装。**
整个项目通过 LangChain 获取的价值仅三项：
- `@tool` 装饰器 → 可用纯函数替代（~20 行）
- `ChatOpenAI` 客户端 → 核心价值，可用 httpx+OpenAI Schema 替代（~100 行）
- `convert_to_openai_function` → 简单 Pydantic schema 导出（~30 行）

LangChain 引入约 200MB 传递依赖。对一个轻量级 QQ 机器人而言，这是不成比例的代价。

**2. 三套依赖从未使用。**
`faiss-cpu`、`psycopg2-binary`、`textual`、`prompt_toolkit` 合计下载体积 >500MB，但对应功能或未实现、或实现后未被引用。`numpy` 反而是真正使用却未声明的依赖。

**3. 异步模式整体良好但存在隐患。**
- 核心链路 (receiver → pipeline → sender) 为全异步，正确使用 `await`
- `tools.py` 正确使用 `ThreadPoolExecutor` 隔离同步 HTTP
- 但 `start_tui.py` 在守护线程中使用独立事件循环（L144-151），bot 异常会被静默吞掉

**4. 测试框架使用不当。**
`tests/test_deepseek_tools.py` 不是单元测试（无 test 函数、无断言），而是含硬编码 API Key 的手动集成脚本。

---

## 三、技术栈契合度总评

| 维度 | 评分 | 说明 |
|------|------|------|
| 依赖必要性 | ⭐⭐ | 4/12 依赖为死依赖；LangChain 可用更轻方案替代 |
| 依赖完整性 | ⭐⭐ | `numpy` 缺失，`faiss-cpu` 声明但未用 |
| 异步一致性 | ⭐⭐⭐ | 核心链路正确，但启动层有隐患 |
| 框架选择合理性 | ⭐⭐ | LangChain 对轻量 Bot 偏重；TUI 选型摇摆（3 套框架） |
| **综合** | **⭐⭐** | 技术栈需大幅精简；去除死依赖、替换 LangChain 为 httpx |

---

**结论**: 核心技术选型（WebSocket + httpx + Pydantic）契合场景。但 LangChain 过度设计，多套 TUI 框架并存是典型屎山标志——上一任开发者做了三个决定但从未清理。
