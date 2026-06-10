# Mutsumi's SYNC v3 — 项目章程

**状态**: 开发中  
**分支**: `feature/v3-rewrite`  
**起点**: 从 bottle 漂流瓶重建

---

## 1. 项目定位

基于 NapCat 的 QQ 聊天机器人。具备 LLM 对话、工具调用、技能扩展、定时任务的图灵完备 Agent。

## 2. 技术栈

| 组件 | 选型 | 不去用的 |
|------|------|---------|
| 运行时 | Python 3.11+, asyncio | — |
| QQ 接入 | NapCat WebSocket + HTTP API | — |
| LLM | OpenAI 兼容 API (httpx) | ~~LangChain~~ |
| 向量检索 | FAISS | — |
| 配置 | Pydantic + YAML | — |
| 凭证 | `.env` + `config.yaml` (gitignored) | — |
| TUI | **无**（v3 先不做，外部观察者模式） | ~~Textual, prompt_toolkit~~ |

## 3. 架构快速索引

详细设计见 `bottle/docs/`：

| 文档 | 内容 |
|------|------|
| `architecture-for-humans.md` | 完整架构设计书，先读这个 |
| `architecture-for-ai.md` | 结构化架构（供 AI Agent 消费） |
| `overplanning-review.md` | 过度规划评审 + 修正建议 |
| `01~03` | v2 代码评估（历史参考） |

### 核心设计决策复述

1. **Pipeline 是单个异步函数** — `pipeline(message, *, deps)`，全部处理逻辑在内
2. **Scheduler 持有所有状态** — 全局状态 (Config/Tools/Skills/Schedule) + 每用户状态 (Window/Session)
3. **每用户一个 Task** — 新消息取消旧 Task，创建新 Task (`asyncio.Task.cancel()`)
4. **版本号自指** — 工具修改全局状态 → 版本号递增 → Pipeline 比较后重快照
5. **定时任务 = 合成消息** — ScheduleEngine 触发时构造伪消息进入标准 Pipeline
6. **技能 = 数据** — 加载不执行代码；v1 用 Python 模块，YAML 为未来选项

## 4. 模块职责

```
src/mutsumi_sync/
├── main.py          入口：创建 Scheduler，启动 WS
├── scheduler.py     PipelineScheduler：状态持有 + 协程管理
├── pipeline.py      pipeline() 异步函数：端到端消息处理
├── config.py        Config (从 bottle 搬运，去全局单例)
│
├── message/         I/O 层（从 bottle 搬运，几乎不改）
│   ├── receiver.py  WS → MessageEvent
│   ├── sender.py    Peer → HTTP POST
│   └── classifier.py
│
├── tools/           工具系统
│   ├── registry.py  ToolRegistry (版本号)
│   ├── config_manager.py
│   ├── skill_manager.py
│   ├── scheduler_tool.py
│   ├── memory.py
│   ├── system.py
│   └── http_api.py
│
├── skills/          技能系统
│   └── registry.py  SkillRegistry
│
├── memory/          状态存储
│   ├── window.py    MessageWindow (从 bottle 搬运)
│   ├── store.py     MemoryStore (SQLite)
│   └── vector.py    FAISS + embedding
│
├── schedule/
│   └── engine.py    ScheduleEngine
│
└── provider/
    └── llm.py       LLMProvider (httpx + OpenAI API)
```

## 5. 开发阶段

| Phase | 内容 | 产出 |
|-------|------|------|
| **1. 骨架** | PipelineScheduler + pipeline() 骨架 + config + I/O 搬运 | 消息收发可用 |
| **2. 核心** | 6 个内置 Tool + ToolRegistry + 版本号自指 | Agent 自指可用 |
| **3. 扩展** | Skill 系统 + ScheduleEngine | 技能 + 定时任务可用 |
| **4. 补齐** | 向量检索 + 长期记忆 + 测试 | 功能完整 |
| **5. 质量** | CI + 测试覆盖 + 文档 | 可维护 |

## 6. 配置结构（最低要求）

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
  timeout: int
```

## 7. 搬运清单

以下文件从 v2 直接搬运至本项目（已放入 `bottle/src/`），质量合格，复用度高：

| 文件 | 改动 |
|------|------|
| `config.py` | 去全局单例 `_config_instance` |
| `message/receiver.py` | 几乎不改 |
| `message/sender.py` | 几乎不改 |
| `message/classifier.py` | 扩展 IMAGE/MEME 路径 |
| `memory/window.py` | 直接复用 |
