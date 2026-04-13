# Mutsumi's SYNC

QQ LLM 聊天机器人数据中转站

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config.yaml`:

```yaml
napcat:
  ws_url: "ws://localhost:3000"    # napcat WebSocket 地址
  http_url: "http://localhost:3000" # napcat HTTP 地址

model:
  provider: "openai"
  model: "gpt-4"
  temperature: 0.7
  api_key: "your-api-key"          # 设置你的 API Key

context:
  window_size: 20
  max_tokens: 4096

memory:
  pg_connection: "postgresql://user:pass@localhost:5432/mutsumi"
  vector_dim: 1536

deduplication:
  wait_time: 1.0

cache:
  image_md5: "./cache/image_md5.json"
  meme_desc: "./cache/meme_desc.json"
```

### 3. 运行测试

```bash
python -m pytest tests/ -v
```

### 4. 启动机器人

```bash
cd src/mutsumi_sync
python bot.py
```

## 模块说明

| 模块 | 说明 |
|------|------|
| `message/receiver.py` | WebSocket 消息接收 |
| `message/sender.py` | HTTP 消息发送 |
| `message/classifier.py` | 消息分类（短文字/长文字/图片/表情包） |
| `processor/pipeline.py` | LLM 对话管道 |
| `processor/vector.py` | 向量匹配（RAG） |
| `processor/dedup.py` | 消息防抖 |
| `processor/auth.py` | 角色权限管理 |
| `memory/window.py` | 滑动窗口（短期记忆） |
| `memory/postgres.py` | PostgreSQL 长期记忆 |
| `cache/meme.py` | 表情包缓存 |

## 测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定模块测试
python -m pytest tests/test_classifier.py -v
python -m pytest tests/test_vector.py -v
python -m pytest tests/test_dedup.py -v
```

## 注意事项

- 需要先启动 napcat 服务
- LLM API Key 需要自行配置
- PostgreSQL 为可选，长期记忆功能
