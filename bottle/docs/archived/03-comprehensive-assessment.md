# 综合评估报告

**评估日期**: 2026-05-19  
**评估分支**: `main` (HEAD: 67cb628)  
**项目**: Mutsumi's SYNC — 基于 NapCat 的 QQ LLM 聊天机器人

---

## 一、综合评估

### 整体结论

**这是一个代码骨架尚可但血肉严重腐烂的项目**，当前技术水平属于 **"能跑但不可靠的原型"** 阶段。核心链路（WS 接收 → LLM 调用 → HTTP 发送）可工作，但 18 项功能需求中 **6 项完全未实现、3 项有缺陷、9 项正常**。项目由多轮迭代堆砌而成，每轮都留下大量未清理的遗留代码，安全凭证明文提交到仓库，测试几乎不可运行。

### 按维度评分

| 维度 | 评分 (1-5) | 关键依据 |
|------|-----------|---------|
| 功能完成度 | 2.0 | 仅半数功能可用；向量检索/多模态/PostgreSQL/Meme 缓存均为死代码 |
| 代码质量 | 1.5 | 重复方法定义、裸 except、损坏的测试、未定义的函数调用 |
| 架构设计 | 2.5 | 六边形骨架合理，但数据流被回调破坏，3 套 TUI 并存 |
| 技术栈 | 2.0 | 4/12 依赖为死依赖；LangChain 过度设计；numpy 未声明 |
| 安全性 | 1.0 | API Key 明文提交 Git；无权限门控；异常全吞 |
| 可维护性 | 1.5 | 全局单例 config；硬编码服务器路径；无 CI；测试不可运行 |
| 可扩展性 | 3.0 | Tool 可插拔；命令开闭原则；但新增功能易受死代码干扰 |
| **加权综合** | **1.8 / 5.0** | **严重技术债务，需要系统性重构** |

---

## 二、严重问题列表

按严重度排序，从致命到轻微。

### 🔴 CRITICAL — 阻断级（必须立即修复）

#### C-1. API Key 明文提交至仓库
- **位置**: `config.yaml:4,10`; `tests/test_deepseek_tools.py:4,23`
- **影响**: NapCat access_token 和 DeepSeek API Key 裸写在版本控制中，任何有仓库访问权限的人可获取凭证，产生账单盗用和账号劫持风险。
- **修复**: (1) 立即在 DeepSeek 后台轮换 Key；(2) 将 `config.yaml` 加入 `.gitignore`；(3) 创建 `config.example.yaml` 作为模板；(4) 使用 `git filter-branch` 清除历史。

#### C-2. `start.py` 不存在
- **位置**: 项目根目录
- **影响**: README 和 AGENTS.md 均引用 `python start.py` 作为简单启动模式，但当前分支无此文件，用户按文档操作直接报错。
- **修复**: 从 `.worktrees/dev/start.py` 恢复或重新创建。

#### C-3. `tests/test_pipeline.py` 直接损坏
- **位置**: `tests/test_pipeline.py:5-6`
- **影响**: 使用 `model_name="gpt-4"` 参数，但 `ModelPipeline.__init__` 签名是 `model=`，运行必抛 `TypeError`。整个测试套件实际上不可用。
- **修复**: 修正参数名并补全测试逻辑。

#### C-4. `tools.py` 引用未定义函数
- **位置**: `src/mutsumi_sync/processor/tools.py:108`
- **影响**: `asyncio.run(_call())` 中 `_call` 从未定义，若该代码路径被执行将触发 `NameError`。目前该路径因提前 `return` 不可达，但作为死炸弹随时可能被激活。
- **修复**: 将 `_call()` 改为 `_sync_http_call()` 或删除该路径。

---

### 🟠 HIGH — 高风险（短期内必须修复）

#### H-1. 去重逻辑完全不工作
- **位置**: `src/mutsumi_sync/processor/dedup.py:22-34`; `bot.py:74`
- **影响**: `should_reply()` 永远返回 `True`，意味着用户连续发送消息时，每一条都会触发独立的 LLM 调用和回复，造成消息轰炸和 API 费用浪费。`schedule_reply()` 实现的正确逻辑从未被调用。
- **修复**: 将 `schedule_reply()` 接入 `bot.py` 的消息处理流，在首次消息时启动定时器，到期前的新消息重置定时器，到期后才执行回复。

#### H-2. 对话存储未记录 Bot 回复
- **位置**: `start_tui.py:138`
- **影响**: `storage.add_message()` 中 `bot_msg=""` 硬编码为空字符串，TUI 的对话浏览器永远看不到 Bot 的回复内容。`ConversationsCommand` 也是 TBD 占位符。
- **修复**: 在 `wrapped_handle` 中捕获 Bot 的响应并填入 `bot_msg`；或将 `add_message` 拆分为 `add_user_msg` + `add_bot_msg`。

#### H-3. 异常被静默吞噬（3 处）
- **位置**: `start_tui.py:74` (`except: pass`); `src/mutsumi_sync/tui/app.py:48,55` (两个 `except:`)
- **影响**: 任何异常（包括 `KeyboardInterrupt`、`SystemExit`）都被无声吞掉，状态栏永远显示离线，问题无法排查。
- **修复**: 至少记录异常日志；使用具体异常类型而非裸 `except`。

#### H-4. 三套 TUI 碎片化，大量死代码
- **位置**: `start_tui.py` (SimpleREPL 实际使用); `tui/repl.py` (175 行未导入); `tui/app.py` (108 行未导入); `tui/screens/detail.py` (40 行未导入); `tui/widgets/` (72 行未导入)
- **影响**: ~400 行无引用代码使项目结构混乱，新人无法确定真正的代码路径。Textual 和 prompt_toolkit 两个框架仍在 `requirements.txt` 中。
- **修复**: 选定一套 TUI 方案（建议保留 `repl.py` 并修复），删除未使用的 `app.py`、`screens/`、`widgets/` 及对应依赖。

#### H-5. IMAGE/MEME 消息被静默丢弃
- **位置**: `bot.py:88`
- **影响**: `if classified.msg_type in (MessageType.SHORT_TEXT, MessageType.LONG_TEXT)` 仅处理文本，用户发送的图片无任何响应——无回复、无日志、无错误提示。
- **修复**: 添加 IMAGE/MEME 分支：至少回复预设消息（如"收到图片"），或接入 MemeCache 查找描述。

#### H-6. 启动线程中异常被吞
- **位置**: `start_tui.py:144-151`
- **影响**: Bot 在独立守护线程 + 独立事件循环中运行。若 Bot 因异常崩溃，守护线程静默死亡，主线程的 SimpleREPL 继续运行但状态栏永远显示离线，用户完全不知道服务已死。
- **修复**: 使用 `asyncio.run_coroutine_threadsafe()` 共享事件循环；或至少在线程中捕获异常并通过回调通知主线程。

---

### 🟡 MEDIUM — 中风险（本迭代应修复）

#### M-1. 未使用的依赖
- `faiss-cpu` — 零导入（需求中存在但未实现）
- `python-dotenv` — 零导入（`.env` 文件未被使用）
- `psycopg2-binary` — 仅在被调用的类中懒加载，但该类永不实例化
- 删除可减少 ~200MB 安装体积。

#### M-2. 缺失依赖 `numpy`
- `src/mutsumi_sync/processor/vector.py:2` 导入 `numpy` 但 `requirements.txt` 未声明。当前因 `vector.py` 是死代码才未被触发，但依赖声明不完整。

#### M-3. 硬编码服务器路径
- `tests/test_deepseek_tools.py:7` 使用 `/home/ubuntu/gits/mutsumi-sync` 无效于本地
- `tui/repl.py:49` 默认 `log_path` 硬编码服务器路径
- 项目无法在任何非生产机器上完整运行。

#### M-4. Pipeline 降级解析脆弱
- `processor/pipeline.py:198-209` 用 5 个试探性正则表达式猜测模型输出中的 Tool 调用意图，无任何单元测试。模型的输出格式变化时静默失败。
- 建议：改为要求模型输出标准 JSON 块（```json...```），用更稳健的解析。

#### M-5. `config.yaml` 含拼接凭据字符串
- `config.py:28` 默认值包含 `"postgresql://user:pass@localhost:5432/mutsumi"`，虽然是占位符，但 `user:pass` 可能在扫描工具中触发假阳性告警。

#### M-6. 缺少 `tests/__init__.py` 和 CI
- `tests/` 目录缺少 `__init__.py`，部分测试运行器可能无法发现测试。无 CI 配置文件（`.github/workflows/` 或类似），测试从未自动运行。

---

### 🟢 LOW — 低风险（可在重构中顺便修复）

#### L-1. `start_tui.py` 重复方法定义
- L53-55：两个 `def _setup_commands(self):`，第一个为空体。合并遗留产物。

#### L-2. `repl.py` 重复属性赋值
- L49 和 L61 对 `self._log_path` 赋值两次，第二次是死代码。

#### L-3. `app.py` 空处理函数
- L107-108：`action_back(self): pass` — Escape/返回键绑定无实际效果。

#### L-4. `status.py` 状态永远离线
- L19-20：`ai_online = False; napcat_online = False` 硬编码，无论真实状态如何都显示离线。

#### L-5. 方法内 `import time`
- `bot.py:66` 和 `pipeline.py:142-143,195-196` 在方法体内 import 标准库/LangChain。应移至模块顶部。

#### L-6. 全局单例 Config
- `config.py:77` 使用模块级 `_config_instance`，使测试难以隔离不同的配置。

#### L-7. Deque 溢出时丢失最老消息
- `memory/window.py` 使用 `maxlen=20` 的 deque，存入第 21 条时最旧消息被静默丢弃，上下文中出现断层。可接受但应记录日志。

---

## 三、可修复程度评估

### 修复路线

| 阶段 | 工作量 | 内容 |
|------|--------|------|
| **Phase 1: 止血** | 1-2 天 | 轮换 API Key、清理 Git 历史、修复 `start.py` 缺失、修复去重逻辑、补异常处理 |
| **Phase 2: 功能补齐** | 3-5 天 | 接入 FAISS 向量检索、实现 IMAGE/MEME 处理路径、接入 PostgreSQL、实现多模态输出 |
| **Phase 3: 结构清理** | 2-3 天 | 选择并保留一套 TUI、删除另两套 + 死依赖、统一异步事件循环、修复测试 |
| **Phase 4: 工程质量** | 2-3 天 | 添加 CI、补全测试覆盖、替换 LangChain 为 httpx、提取 `.env` 环境变量管理 |

### 各模块可修复程度

| 模块 | 可修复度 | 建议策略 |
|------|---------|---------|
| `message/` (收发层) | ⭐⭐⭐⭐⭐ | **保留**。代码质量好，接口清晰，几乎无需修改 |
| `config.py` | ⭐⭐⭐⭐ | **保留**。结构良好，只需移除全局单例改为依赖注入 |
| `memory/window.py` | ⭐⭐⭐⭐⭐ | **保留**。极简且正确 |
| `processor/pipeline.py` | ⭐⭐⭐ | **大幅重构**。核心逻辑正确但降级解析需重写，回调需消除 |
| `processor/tools.py` | ⭐⭐⭐ | **小幅重构**。删除死代码路径，LangChain @tool 可替换 |
| `processor/vector.py` | ⭐⭐ | **重写**。当前为死代码 stub，需替换为真正的 FAISS + Embedding 生成 |
| `processor/dedup.py` | ⭐⭐⭐⭐ | **修复**。逻辑本身正确，只是 `schedule_reply()` 未接入流程 |
| `processor/auth.py` | ⭐⭐⭐⭐ | **接入**。类本身无问题，只需在 `bot.py` 中添加权限检查调用 |
| `cache/meme.py` | ⭐⭐⭐⭐ | **接入**。类本身正确，需在 IMAGE 处理路径中调用 `.get()` |
| `memory/postgres.py` | ⭐⭐⭐ | **重写**。使用 psycopg2 同步接口在异步环境中不佳，建议换 asyncpg |
| `tui/commands/` | ⭐⭐⭐⭐ | **保留**。设计良好，可复用 |
| `tui/repl.py` | ⭐⭐⭐ | **保留并修复**。prompt_toolkit 方案实现最完整，修复路径硬编码和状态更新后可直接使用 |
| `tui/app.py` + `screens/` + `widgets/` | ⭐ | **删除**。从未使用，功能与 repl.py 高度重叠 |
| `start_tui.py` | ⭐⭐ | **重写**。混合了入口 + SimpleREPL + 线程管理，应拆分 |

### 可修复度总评

| 指标 | 评估 |
|------|------|
| 核心架构 | 可修复 — 六边形骨架清晰，无需推倒重来 |
| 收发层 | 可直接复用，几乎零修改 |
| Pipeline | 需要中等规模重构（降级解析 + 消除回调） |
| 向量/记忆/缓存 | 需要从 stub 补全为完整实现 |
| TUI | 需要收敛到单一方案并清理约 400 行死代码 |
| 测试 | 基本从零开始 — 现存测试中仅 4 个文件可正常运行 |
| 安全 | 凭证轮换 + 环境变量提取即可解决 |
| **整体可修复度** | **75% — 值得修复但需要 8-13 人天** |

---

## 四、最终建议

**不建议推倒重来。** 项目的收发层（`message/`）、配置系统、命令模式设计是可靠的。问题集中在：
1. 大量功能实现后未接入主流程（死代码问题）
2. 多轮技术选型摇摆（3 套 TUI + LangChain 过重）
3. 安全和工程质量缺失（密钥泄露、无 CI、测试损坏）

**建议按 Phase 1→4 顺序执行修复**，Phase 1 止血后可快速恢复生产可用性，后续阶段逐步偿还技术债务。若团队资源有限，Phase 1 大约 1-2 天即可让项目回归安全工作状态。

对于新接手者，**最关键的第一步**是在可工作代码和死代码之间建立清晰的隔离——把 `bot.py` 中 6 个实例化但不使用的对象标为 TODO，把未导入的 TUI 文件移入 `_archived/` 目录，让项目结构真实反映当前工作状态。
