# Mutsumi's SYNC — v3 漂流瓶

这是 v2 代码库处理后留下的**全部有用产出物**，作为 v3 重写的起点。

## 内容

```
bottle/
├── README.md
├── docs/
│   ├── architecture-for-ai.md          # 架构设计（AI Agent 消费版）
│   ├── architecture-for-humans.md      # 架构设计（人类阅读版）
│   ├── overplanning-review.md          # 过度规划评审
│   ├── 01-requirements-techstack.md    # 需求完成度 + 技术栈评估
│   ├── 02-architecture.md              # 旧架构反模式分析
│   └── 03-comprehensive-assessment.md  # 综合评估 + 修复路线
└── src/
    └── mutsumi_sync/
        ├── config.py                   # Pydantic 配置模型（→ 保留基础，移除全局单例）
        ├── message/
        │   ├── receiver.py             # WebSocket 接收 + 重连
        │   ├── sender.py               # HTTP 发送
        │   └── classifier.py           # 消息分类
        └── memory/
            └── window.py               # 滑动窗口 (deque)
```

## 搬运代码说明

| 文件 | 原行数 | 质量 | v3 处理方式 |
|------|--------|------|------------|
| `config.py` | 103 | 良好 | 保留 Pydantic 模型，去掉全局单例 |
| `receiver.py` | 112 | 良好 | 几乎直接复用 |
| `sender.py` | 81 | 良好 | 几乎直接复用 |
| `classifier.py` | 44 | 良好 | 扩展 IMAGE/MEME 路径 |
| `window.py` | 18 | 良好 | 直接复用 |

## 旧代码状态

- Git tag: `archive/legacy` — 完整 v2 代码永久存档
- 新分支: `feature/v3-rewrite` — 只含本漂流瓶内容

## 开发路线

1. 从本漂流瓶搬运代码开始
2. 按 `architecture-for-humans.md` 实现 PipelineScheduler + pipeline()
3. 实现 6 个内置 Tool
4. 接入 Skill 系统
5. 实现 ScheduleEngine
6. 补测试
