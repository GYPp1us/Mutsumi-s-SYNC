# Mutsumi's SYNC 阶段报告

> 更新日期：2026-04-15

## 项目概述

Mutsumi's SYNC 是一个基于 NapCat QQ 框架的 LLM 聊天机器人，支持函数调用、多轮对话、配置管理等功能。

## 本次更新内容

### 1. 函数调用（Tool Calling）支持

- 使用 LangChain `@tool` 装饰器定义工具
- 支持多轮 Tool 调用（最多 5 轮）
- 内置两个工具：
  - **config_manager**: 配置管理（get/set/list/reload）
  - **http_api_call**: 通用 HTTP 请求

### 2. Fallback 解析机制

- 当模型不支持原生 Tool Calls 时，解析 content 中的 JSON 格式调用
- 支持多种格式：纯 JSON、`config_manager({...})`、`action: call_tool` 等

### 3. 异步 HTTP 调用修复

- 解决 `asyncio.run()` 在已运行事件循环中的错误
- 使用 ThreadPoolExecutor 在异步上下文中执行同步 HTTP 请求

### 4. 配置化 System Prompt

- 在 `config.yaml` 中添加 `system_prompt` 配置项
- 可自定义 AI 人格和行为指令

### 5. 详细调试日志

- 记录发送到模型的完整 prompt
- 记录模型返回的完整 response
- 记录 Tool 调用和执行结果

## 技术细节

### 配置文件 (config.yaml)

```yaml
model:
  provider: "deepseek"
  model: "deepseek-chat"  # 推荐使用 chat 模型
  api_key: "sk-xxx"
  base_url: "https://api.deepseek.com/"

system_prompt: |
  你是一个友好、热情的 AI 助手...
  ## 可用工具
  1. config_manager - 配置管理
  2. http_api_call - HTTP API 调用
```

### 模型兼容性

| 模型 | 原生 Tool Calls | Fallback 解析 |
|------|-----------------|---------------|
| deepseek-chat | ✅ 支持 | ✅ 可用 |
| deepseek-reasoner | ⚠️ 部分支持 | ⚠️ 格式不稳定 |

**推荐使用 `deepseek-chat`**

### 核心代码结构

```
src/mutsumi_sync/
├── bot.py                  # 主机器人
├── config.py              # 配置加载（含 save/reload）
├── message/
│   ├── receiver.py        # WebSocket 消息接收
│   ├── sender.py          # HTTP 消息发送
│   └── classifier.py      # 消息分类
└── processor/
    ├── pipeline.py        # LLM 对话管道（含 Tool 调用）
    ├── tools.py           # Tool 定义
    ├── vector.py          # 向量匹配
    ├── dedup.py           # 消息防抖
    └── auth.py            # 权限管理
```

## 测试结果

```bash
# Tool 调用测试
1. config_manager: ✅ 返回 {"model.model": "gpt-4o-mini"}
2. http_api_call: ✅ 返回 HTTP 响应
```

## 已知问题

- deepseek-reasoner 的 fallback 解析需要针对更多输出格式适配
- 后续需要测试真实的 NapCat 消息收发流程

## 下一步计划

1. 集成 NapCat 完整消息收发测试
2. 向量匹配功能完善
3. 多模态输出支持（图片、表情包）
4. PostgreSQL 长期记忆集成