# Mutsumi's SYNC

## 项目定位

QQ LLM 聊天机器人领域的数据中转站，轻量、完备、拟人。针对 `astr bot` 过重、不够拟人的问题提供替代方案。

## 技术栈

- **语言**：Python 3.10+
- **消息队列**：asyncio + Redis（可选）
- **向量数据库**：FAISS + pgvector (PostgreSQL)
- **LLM 框架**：LangChain（支持 Tool 装饰器实现函数调用）
- **HTTP 客户端**：httpx

## 系统架构（数据流）

```
消息接收 ──> 消息分类 ──> 内容处理 ──> 回复发送
                │              │
           文字/图片        ├─ 向量匹配（补充模式）
                           ├─ 模型 Pipeline + Tool 调用
                           └─ 配置管理 Tool
```

## 核心模块

### 1. 消息接收

- 监听 napcat WebSocket API
- 解析消息结构：群号/私聊、用户ID、消息内容、消息类型

### 2. 消息分类

| 消息类型 | 处理方式 |
|----------|----------|
| 短文字 | 向量数据库匹配（补充模式） |
| 长文字 | 启动模型 Pipeline |
| 图片 | md5 缓存匹配 → OCR/描述模型 → 存入缓存 |
| 表情包 | 直接匹配文字描述缓存 |

### 3. 向量匹配（补充模式）

- 查询向量库，未命中时走模型 Pipeline
- 命中文本作为上下文补充，而非直接回复

### 4. 模型 Pipeline

```
接收消息 ──> 拼接上下文 ──> 添加 System Prompt ──> 调用 LLM ──> Tool 执行 ──> 格式化输出 ──> 发送回复
```

#### Tool 函数调用（LangChain）

- 使用 `@tool` 装饰器定义工具
- 支持多轮调用：模型可连续调用多个 Tool 直到完成任务
- 内置工具：
  - **config_manager**: 配置管理（查看/修改配置）
  - **http_api_call**: 通用 HTTP API 调用
- 工具通过 System Prompt 自动注册

#### 多模态输出拼接

- **支持格式**：纯文字、文字+图片、纯图片、表情包、引用回复
- **拼接规则**：模型输出需指定输出类型，系统将结果转换为 napcat 可识别的消息段 (CQ码)
- **图片处理**：支持 base64 / URL / 文件路径三种方式引用图片

#### 上下文管理

- **短期记忆**：滑动窗口保留最近 N 条消息（可配置）
- **长期记忆**：PostgreSQL 存储历史会话，支持按时间/用户检索
- **向量库**：FAISS 用于快速相似度匹配

### 5. 消息防抖

| 场景 | 策略 |
|------|------|
| 向量数据库命中 | 立刻回复 |
| 表情包缓存命中且其对应的描述在向量数据库中命中 | 立刻回复 |
| 需模型处理 | 等待窗口时间（可配置），期间被新消息打断则重置计时 |

### 6. 角色权限

- **管理员**：可配置系统、查看日志、管理向量库
- **普通用户**：触发聊天交互

## napcat API 对接

- 消息接收：WebSocket 监听
- 消息发送：HTTP API 调用
- 参考文档：https://napneko.github.io/api/4.17.53

## 配置项

```yaml
 napcat:
   ws_url: "ws://localhost:3000"
   http_url: "http://localhost:3000"

 model:
   provider: "openai"  # 或 anthropic/本地模型
   model: "gpt-4"
   temperature: 0.7

 context:
   window_size: 20     # 滑动窗口大小
   max_tokens: 4096    # 最大 token 数

 memory:
   pg_connection: "postgresql://user:pass@localhost:5432/mutsumi"
   vector_dim: 1536    # 向量维度

 deduplication:
   wait_time: 1.0      # 防抖等待时间（秒）

 cache:
   image_md5: "./cache/image_md5.json"
   meme_desc: "./cache/meme_desc.json"

 system_prompt: |
   你是一个友好、热情的 AI 助手...
```

## 人格设定

由用户在 System Prompt 中自定义，机器人根据用户配置的 persona 进行对话。

## 扩展性

- 函数调用能力通过 LangChain @tool 装饰器定义
- 支持自定义工具：天气查询、百科搜索、API 转发等
- 模块化设计，便于替换底层组件（如换用不同的 LLM 提供商）
