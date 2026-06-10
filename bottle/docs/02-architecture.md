# 架构合理性评估

**评估日期**: 2026-05-19  
**评估分支**: `main` (HEAD: 67cb628)

---

## 一、模块依赖拓扑

```
start_tui.py (实际入口)
  ├── config.py ──── 配置加载（pydantic + YAML）
  ├── bot.py ─────── 中央编排器 ★
  │   ├── message/receiver.py ── WebSocket 接收
  │   ├── message/classifier.py ─ 消息分类
  │   ├── message/sender.py ──── HTTP 发送
  │   ├── processor/pipeline.py ─ LLM + Tool 调用
  │   │   └── processor/tools.py ── config_manager, http_api_call
  │   │       └── config.py (循环风险: processor → root config)
  │   ├── processor/vector.py ─── 向量匹配（numpy，死代码）
  │   ├── processor/dedup.py ──── 去重（逻辑缺陷）
  │   ├── processor/auth.py ──── 权限（未接入）
  │   ├── cache/meme.py ──────── 表情包缓存（未接入）
  │   └── memory/window.py ──── 滑动窗口（正常）
  └── tui/
      ├── storage.py ─────────── 对话记录
      ├── commands/ ──────────── 命令系统（共 8 个命令）
      ├── app.py ─────────────── Textual TUI（死代码）
      ├── repl.py ────────────── prompt_toolkit REPL（死代码）
      ├── theme.py ───────────── Solarized Dark 主题
      ├── screens/detail.py ──── 详情页（死代码）
      └── widgets/ ───────────── 对话/状态栏组件（死代码）
```

### 依赖违规

| # | 违规 | 涉及文件 | 严重度 |
|---|------|---------|--------|
| 1 | **双向耦合**: Summary 回调使 Pipeline → Bot 形成反馈环 | `bot.py:38` → `pipeline.py:261` → `bot.py:48` | 中 |
| 2 | **越层访问**: Pipeline 持有 `Peer` 对象（传输层类型） | `pipeline.py:137` 参数 `peer: Any` 实际是 `sender.Peer` | 低 |
| 3 | **跨包反向依赖**: `processor/tools.py` → `..config.py` | `tools.py:3` | 低（无实际循环） |

---

## 二、消息处理流分析

### 当前实际流（`bot.py handle_message()`）

```
                    ┌─────────┐
                    │ Receiver│ (WebSocket 事件)
                    └────┬────┘
                         │ MessageEvent
                    ┌────▼────┐
                    │classify │ (同步，仅 TEXT 生效)
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  dedup  │ should_reply() 永远返回 True
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  cold?  │ 超时 → send_poke
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  vector │ 仅调用 is_empty()，无实际检索
                    └────┬────┘
                         │
              ┌──────────▼──────────┐
              │ msg_type in {SHORT, │  IMAGE / MEME 被静默丢弃
              │ LONG_TEXT} ?        │
              └──────┬──────┬──────┘
                     │ YES  │ NO → 消息被吞，无任何响应
              ┌──────▼──────┐
              │   window    │ 获取滑动窗口上下文
              └──────┬──────┘
                     │
              ┌──────▼──────┐
              │  pipeline   │ LLM + Tool 多轮调用 (max 5)
              │   .chat()   │ 含 ||...|| 摘要回调 → sender
              └──────┬──────┘
                     │ response: str
              ┌──────▼──────┐
              │   sender    │ HTTP POST send_group/private_msg
              └──────┬──────┘
                     │
              ┌──────▼──────┐
              │   window    │ 追加 user + bot 消息
              └─────────────┘
```

### 架构缺陷

**A. 死胡同路径**
- `IMAGE` 类型消息进入分类器后，在 L88 的 `if classified.msg_type in (...)` 处被静默丢弃。用户发图片无任何响应。
- 分类器定义了 `MEME` 枚举值但从不赋值，相关路径完全断裂。

**B. 向量捷径未兑现**
- 根据设计规范：短文本应先查询向量库，命中则直接回复，未命中才走 LLM。
- 实际：所有文本无差别走 LLM pipeline。`VectorMatcher.search()` 从不被调用。

**C. 去重是空操作**
- `Deduplicator.should_reply()` 在任何情况下都返回 `True`（`bot.py:32` + `bot.py:34`）。
- 正确行为应为：首次消息设置定时器，定时器到期前的新消息重置定时器，到期后才回复。
- `schedule_reply()` 实现了定时器逻辑但从未接入流中。

**D. Pipeline 对 Bot 的反向调用**
- `summary_callback` 使 Pipeline 在执行 Tool 时通过回调通知 Bot 发送 `||摘要||` 给用户。
- 这打破了单向数据流：`Receiver → Pipeline → Sender`。
- 正确的设计应把摘要作为 pipeline 的返回值的一部分，由 Bot 自行判断是否发送。

---

## 三、模块设计质量

### 优秀模块

| 模块 | 文件 | 评价 |
|------|------|------|
| 配置系统 | `config.py` | Pydantic 模型清晰，点号路径遍历完善，`save()`/`reload()` 支持热更新 |
| 命令系统 | `tui/commands/` | 抽象基类 + Registry 模式，开闭原则良好，子命令补全支持 |
| 滑动窗口 | `memory/window.py` | 极简实现（18 行），deque 天然适配，职责单一 |

### 问题模块

| 模块 | 文件 | 问题 |
|------|------|------|
| TUI 入口 | `start_tui.py` | L53-55 重复方法定义（合并遗留物）；L74 `except: pass` 吞异常；L144-151 守护线程中独立事件循环不可靠 |
| Pipeline | `processor/pipeline.py` | L194-243 降级 JSON 解析用多个试探性正则，脆弱且无测试；L142-143 方法内 import；L286 `str(result)` 无截断；`_llm` 缓存不失效 |
| Tools | `processor/tools.py` | L108 `asyncio.run(_call())` — `_call` 从未定义，触发 `NameError`；该路径不可达但仍为死代码 |
| 向量匹配 | `processor/vector.py` | 仅 30 行 numpy 裸实现，非 FAISS；`search()` 返回 `(text, similarity)` 但无调用方。没有 Embedding 生成代码 |
| 对话存储 | `tui/storage.py` | `add_message()` 创建 Round 时 `bot_msg=""` 硬编码为空（`start_tui.py:138`），bot 回复永不记录 |

### 测试覆盖

| 文件 | 行数 | 状态 |
|------|------|------|
| `tests/test_pipeline.py` | 13 | **损坏** — 使用 `model_name=` 参数但构造函数签名是 `model=`，必抛 TypeError |
| `tests/test_deepseek_tools.py` | 50 | **不是测试** — 零 test 函数、零断言，是手动集成脚本 |
| `tests/test_vector.py` | 20 | 正常 — 但被测代码本身是死代码 |
| `tests/test_classifier.py` | 20 | 正常 — 3 用例 |
| `tests/test_dedup.py` | 23 | 正常 — 但命名误导（`test_dedup_cancel` 不测 cancel） |
| `tests/test_receiver.py` | 19 | 正常 — 仅测 Pydantic 解析 |
| `tests/test_repl.py` | 37 | 正常 — 覆盖率不完整 |
| `test_curses.py` | 5 | 游离 — 不在 `tests/` 目录 |
| `test_storage.py` | 19 | 游离 — 不在 `tests/` 目录 |
| `test_tui.py` | 7 | 游离 — 不在 `tests/` 目录 |

结论：**无 `tests/__init__.py`**，无 CI 配置，`test_pipeline.py` 直接损坏无法运行，实际有效测试覆盖率 < 10%。

---

## 四、关键架构反模式

### 反模式 1: 三套 TUI 并存

```
SimpleREPL (start_tui.py)  ← 实际使用
REPL (tui/repl.py)         ← 从未导入
MutsumiTUI (tui/app.py)    ← 从未导入
```

命令实现在 `tui/commands/` 中被 SimpleREPL 和 REPL 共享；但 Textual 侧的 `app.py` 和 `screens/detail.py` 与命令系统完全隔离。这是典型的多轮迭代未清理的屎山印记——开发者尝试了 Textual → prompt_toolkit → 回到 bare input()，每轮都留下了残留代码。

### 反模式 2: 实例化但不使用的对象

```python
# bot.py:40-43 — 四个对象构造后从不调用方法
self.matcher = VectorMatcher(...)      # 仅调用 is_empty()
self.dedup = Deduplicator(...)         # should_reply() 无实际效果
self.auth = AuthManager()              # 零调用
self.meme_cache = MemeCache(...)       # 零调用
```

### 反模式 3: 静态配置中的安全凭证

`config.yaml` 中明文包含 NapCat `access_token` 和 DeepSeek `api_key`，且已提交至 Git 仓库。`tests/test_deepseek_tools.py` 同样硬编码 `api_key`。这是一级安全漏洞。

### 反模式 4: 硬编码服务器路径

```python
# tests/test_deepseek_tools.py:7
sys.path.insert(0, "/home/ubuntu/gits/mutsumi-sync")

# tui/repl.py:49
log_path="/home/ubuntu/gits/mutsumi-sync/logs/mutsumi.log"
```

这些路径仅在生产服务器上有效，导致项目无法在其他环境运行测试。

---

## 五、架构总评

| 维度 | 评分 | 说明 |
|------|------|------|
| 模块划分 | ⭐⭐⭐ | 6 个子包职责分明，接口清晰 |
| 数据流设计 | ⭐⭐ | 核心流单向但 Pipeline 存在反向回调；IMAGE/MEME 路径断裂 |
| 代码复用 | ⭐⭐ | 命令模式复用良好；但 3 套 TUI 共享零代码（命令除外） |
| 可测试性 | ⭐ | 全局单例 config、test 损坏、无 CI |
| 安全性 | ⭐ | API Key 明文提交、异常全吞、无权限门控 |
| 扩展性 | ⭐⭐⭐ | Tool 框架可插拔；命令系统开闭原则 |
| **综合** | **⭐⭐** | 模块骨架合理，但内部血肉严重腐烂 |

---

**结论**: 项目具有一个还算清晰的六边形架构骨架，但大部分垂直功能要么（a）实现后从未接入流中，要么（b）实现逻辑有根本缺陷。当前可工作的功能实质上只有：**接收文本消息 → LLM 回复 → 发送文本**，其他 10+ 功能均为不同程度的死代码。
