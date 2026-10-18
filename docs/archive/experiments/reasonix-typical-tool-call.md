# Reasonix 一次典型工具调用的完整原文与交互流

> **历史观察记录（2026-09-08）**：本文已移入归档，只描述外部编码工具的一次行为，不是 Mutsumi's SYNC 的运行时契约。当前清理和设计入口见 [`../../repository-cleanup-and-experiment-plan-2026-09-08.md`](../../repository-cleanup-and-experiment-plan-2026-09-08.md)。

本文只回答一个问题：模型为什么能在工具调用前先说一句“我先读取一下文件”。

先给结论：这句话和工具调用属于**同一次模型回答**。模型先在回答的 `content` 字段里生成这句可见文字，随后在同一个回答里生成 `tool_calls`。Reasonix 收到可见文字就显示出来；等这一轮回答完整结束后，才真正执行工具。

下面是一轮完整示例。消息结构和执行顺序来自 Reasonix `main-v2` 当前源码；为便于阅读，具体中文内容、调用编号和 `main.go` 的内容是构造的，地址、路径和密钥也不是本机真实值。

## 场景

用户输入：

```text
帮我看看 main.go 里程序入口是怎么组织的。
```

## 第一次请求：让模型决定下一步做什么

Reasonix 向模型服务发送的内容，可以简化为下面这份原文。实际请求中的系统提示词和工具定义更长，但传输结构就是这样：

```json
{
  "model": "deepseek-reasoner",
  "stream": true,
  "messages": [
    {
      "role": "system",
      "content": "你是一个编码助手。你可以读取和修改工作区文件。需要使用工具时，先简短告诉用户你准备做什么，然后调用工具。"
    },
    {
      "role": "user",
      "content": "帮我看看 main.go 里程序入口是怎么组织的。"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取指定文件",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {
              "type": "string",
              "description": "要读取的文件路径"
            }
          },
          "required": ["path"]
        }
      }
    }
  ]
}
```

这里没有单独请求一次模型来生成“我先读取一下”。Reasonix 只发了上面这一次请求。

## 第一次响应：一句话和工具调用一起回来

模型使用流式响应。下面保留了服务端逐段返回时最关键的原文形式：

```text
data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"用户要了解 main.go 的入口结构，我需要先读取该文件。"}}]}

data: {"choices":[{"delta":{"content":"我先读取 main.go。"}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":""}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"path\":"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"main.go\"}"}}]}}]}

data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

按人能直接理解的顺序，就是：

```text
1. 模型内部思考：需要先读取 main.go。
2. 模型输出给用户看的话：我先读取 main.go。
3. 模型提出工具调用：read_file。
4. 模型逐段补齐工具参数：{"path":"main.go"}。
5. 模型声明这一轮因为要调用工具而结束。
```

其中 `reasoning_content` 是模型的隐藏思考，不显示给用户；`content` 是普通可见文字，所以“我先读取 main.go。”会出现在界面上。

## 此时用户看到什么

Reasonix 在接收流式响应时，会立刻把 `content` 中的文字交给终端显示。因此用户先看到：

```text
我先读取 main.go。
```

当工具名称到达后，Reasonix 还会根据工具调用数据画出工具状态，例如：

```text
  -> read_file({"path":"main.go"})
```

合起来看起来就是：

```text
我先读取 main.go。
  -> read_file({"path":"main.go"})
```

第一行是**模型生成的普通回复**。第二行是**Reasonix 根据工具调用生成的界面状态**，不是模型额外说的一句话。

工具名称刚到达时，参数可能还没传完。Reasonix 会先创建一个工具状态，后续收到参数分片时再更新它，所以界面可以很早就显示工具正在准备中。

## 第一轮结束后，Reasonix 才真正执行工具

Reasonix 会先把刚才收到的各个分片合成一条完整的助手消息：

```json
{
  "role": "assistant",
  "content": "我先读取 main.go。",
  "reasoning_content": "用户要了解 main.go 的入口结构，我需要先读取该文件。",
  "tool_calls": [
    {
      "id": "call_1",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\":\"main.go\"}"
      }
    }
  ]
}
```

然后才执行：

```text
read_file({"path":"main.go"})
```

假设工具读取成功，结果会作为一条工具消息加入会话：

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "name": "read_file",
  "content": "package main\n\nfunc main() {\n    app := NewApp()\n    app.Run()\n}\n"
}
```

所以，工具执行发生在“我先读取 main.go。”已经生成并显示之后。

## 第二次请求：把工具结果交还给模型

为了让模型根据文件内容回答用户，Reasonix 随后发起第二次模型请求。这次会把第一轮的助手消息和工具结果一起带上：

```json
{
  "model": "deepseek-reasoner",
  "stream": true,
  "messages": [
    {
      "role": "system",
      "content": "你是一个编码助手。你可以读取和修改工作区文件。需要使用工具时，先简短告诉用户你准备做什么，然后调用工具。"
    },
    {
      "role": "user",
      "content": "帮我看看 main.go 里程序入口是怎么组织的。"
    },
    {
      "role": "assistant",
      "content": "我先读取 main.go。",
      "reasoning_content": "用户要了解 main.go 的入口结构，我需要先读取该文件。",
      "tool_calls": [
        {
          "id": "call_1",
          "type": "function",
          "function": {
            "name": "read_file",
            "arguments": "{\"path\":\"main.go\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_1",
      "name": "read_file",
      "content": "package main\n\nfunc main() {\n    app := NewApp()\n    app.Run()\n}\n"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取指定文件",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string"}
          },
          "required": ["path"]
        }
      }
    }
  ]
}
```

注意，第二次请求里保留了第一轮的三样东西：

```text
模型刚才对用户说的话
模型刚才提出的工具调用
工具实际返回的结果
```

模型因此知道自己刚才做了什么，也能继续回答，而不是从头开始猜。

## 第二次响应：给出最终答案

第二次响应不再需要工具，典型流式原文如下：

```text
data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"main 函数只负责创建应用并启动，具体初始化被放进 NewApp。"}}]}

data: {"choices":[{"delta":{"content":"程序入口很薄：`main()` 只做两件事，先通过 `NewApp()` 创建应用，再调用 `Run()` 启动。具体的依赖初始化和业务装配不在入口函数里。"}}]}

data: {"choices":[{"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

用户最终看到：

```text
程序入口很薄：`main()` 只做两件事，先通过 `NewApp()` 创建应用，再调用 `Run()` 启动。具体的依赖初始化和业务装配不在入口函数里。
```

## 整个过程压缩成一条时间线

```text
用户提问
  ↓
Reasonix 第一次请求模型
  ↓
模型生成隐藏思考
  ↓
模型在 content 中生成“我先读取 main.go。”
  ↓
Reasonix 立刻显示这句话
  ↓
同一次模型响应继续生成 read_file 工具调用
  ↓
Reasonix 显示工具状态
  ↓
第一轮模型响应结束
  ↓
Reasonix 真正执行 read_file
  ↓
Reasonix 把工具结果加入会话
  ↓
Reasonix 第二次请求模型
  ↓
模型根据文件内容生成最终回答
```

最关键的一句是：**工具前的同步话，是第一次模型响应里的普通可见文字；Reasonix 一边显示它，一边继续接收同一响应里的工具调用，等整轮响应结束后才执行工具。**
