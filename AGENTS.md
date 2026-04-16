# AGENTS.md - Mutsumi's SYNC

## 开发流程

本项目使用以下开发流程，确保高效沟通和问题解决：

### 1. 沟通原则

- **使用中文对话** - 所有交流使用中文
- **不要猜测** - 遇到不确定的问题时，直接提出而不是假设
- **及时提问** - 有疑问立即提出，不要等到最后
- **提供选项** - 当需要决策时，提供 2-3 个选项并推荐

### 2. 工作流程

```
brainstorming → writing-plans → subagent-driven-development → verification → finish
```

| 阶段 | 描述 |
|------|------|
| brainstorming | 探索需求，明确设计 |
| writing-plans | 创建实现计划 |
| subagent-driven-development | 子代理执行任务 |
| verification-before-completion | 完成前验证 |
| finishing-a-development-branch | 完成分支 |

### 3. 开发规范

- **设计先行** - 任何实现前先完成设计文档
- **测试驱动** - 使用 TDD 编写单元测试
- **小步提交** - 频繁提交，保持增量
- **本地测试** - 在开发环境完成测试后再同步

### 4. 服务器同步

开发服务器：`root@code.arcol.site:/home/ubuntu/gits/mutsumi-sync/`

```bash
# 同步文件到远程
scp -o StrictHostKeyChecking=no <local_file> root@code.arcol.site:/home/ubuntu/gits/mutsumi-sync/<remote_path>

# 或者使用 sync.sh 脚本（如果存在）
bash sync.sh
```

### 5. Git 工作流

```bash
# 1. 创建功能分支（新功能）
git worktree add .worktrees/<feature> -b feature/<feature-name>

# 2. 开发完成后的选项
# Option 1: 合并到 main 本地
git checkout main && git merge feature/<feature-name>

# Option 2: 推送到远程
git push -u origin feature/<feature-name>
# 然后手动创建 PR
```

---

## Quick Start

```bash
# 安装依赖
pip install -r requirements.txt

# 启动
python start_tui.py   # TUI 模式（推荐）
python start.py       # 简单模式

# 测试
pytest tests/ -v
```

## Project Structure

```
src/mutsumi_sync/
├── bot.py              # 主机器人
├── config.py           # 配置加载
├── tui/                # 窗口化 TUI
│   ├── app.py         # Textual 主应用
│   ├── storage.py     # 对话存储
│   ├── widgets/       # 组件
│   └── screens/      # 屏幕
├── message/            # WebSocket 接收 & HTTP 发送
│   ├── receiver.py
│   ├── sender.py
│   └── classifier.py
└── processor/         # LLM 管道、工具、权限
    ├── pipeline.py
    ├── tools.py
    ├── vector.py
    ├── dedup.py
    └── auth.py
```

## Running Modes

| 命令 | 描述 |
|------|------|
| `python start_tui.py` | 窗口化 TUI（支持多用户） |
| `python start.py` | 简单日志模式 |

## TUI Controls

| 按键 | 功能 |
|------|------|
| ← → | 切换用户/对话 |
| ↑ ↓ | 切换对话轮次 |
| Enter | 查看详情（Tool Calls） |
| Esc | 返回 |

## Logging

- 文件: `./logs/mutsumi.log` (DEBUG)
- TUI 显示: INFO

## Dependencies

- `langchain` / `langchain-openai` - LLM 框架
- `textual` - 窗口化 TUI
- `httpx` - HTTP 客户端
- `websockets` - WebSocket 通信
- `pytest` - 测试

## Skills

使用 superpowers skills：

- `brainstorming` - 创造性工作前必用
- `subagent-driven-development` - 多任务实现
- `systematic-debugging` - Bug 调试
- `verification-before-completion` - 完成前验证

相关文档：`docs/superpowers/plans/` 和 `docs/superpowers/specs/`