# Mutsumi's SYNC

QQ LLM 聊天机器人，基于 NapCat 框架，支持函数调用和窗口化 TUI。

## 功能特性

- **消息接收** - WebSocket 监听 NapCat 消息
- **消息发送** - HTTP API 发送回复
- **函数调用** - LangChain Tool 装饰器支持多轮函数调用
  - `config_manager` - 配置管理（get/set/list/reload）
  - `http_api_call` - 通用 HTTP 请求
- **窗口化 TUI** - 交互式终端界面
  - 多对话管理（按用户分组）
  - 实时状态监控（AI API、NapCat、消息计数）
  - 详情查看（Tool Calls 详情）

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置

编辑 `config.yaml`：

```yaml
napcat:
  ws_url: "ws://localhost:3001/ws"
  http_url: "http://localhost:3000"
  access_token: "your_token"

model:
  provider: "deepseek"
  model: "deepseek-chat"
  api_key: "your_api_key"
  base_url: "https://api.deepseek.com/"

system_prompt: |
  你是一个友好、热情的 AI 助手...

context:
  window_size: 20
  max_tokens: 4096
```

### 运行

```bash
# 启动 TUI（窗口化界面）
python start_tui.py

# 或启动简单模式
python start.py
```

## TUI 操作

| 按键 | 功能 |
|------|------|
| ← → | 切换用户/对话 |
| ↑ ↓ | 切换对话轮次 |
| Enter | 查看详情 |
| Esc | 返回 |

## 项目结构

```
src/mutsumi_sync/
├── bot.py              # 主机器人
├── config.py           # 配置加载
├── message/
│   ├── receiver.py    # WebSocket 接收
│   ├── sender.py      # HTTP 发送
│   └── classifier.py  # 消息分类
├── processor/
│   ├── pipeline.py    # LLM 对话管道
│   ├── tools.py       # Tool 定义
│   ├── vector.py      # 向量匹配
│   ├── dedup.py       # 消息防抖
│   └── auth.py        # 权限管理
├── tui/               # 窗口化 TUI
│   ├── app.py         # 主应用
│   ├── storage.py     # 对话存储
│   ├── widgets/       # 组件
│   └── screens/       # 屏幕
├── memory/
│   ├── window.py     # 滑动窗口
│   └── postgres.py   # PostgreSQL
└── cache/
    └── meme.py       # 表情包缓存
```

## 日志

- 日志文件: `./logs/mutsumi.log`
- 级别: DEBUG（写入文件）/ INFO（TUI 显示）

## 测试

```bash
pytest tests/ -v
```

## 许可证

MIT