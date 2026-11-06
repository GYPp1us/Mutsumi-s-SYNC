# 长期资料、持久投递与官方 QQ 可选接入

2026-11-06 实现，数据库升级为 schema 5；旧事件、角色、模型配置与报告注册均保留。

## 笔记与完整历史

`events` 继续保存完整原始历史；`notes` 和不可变 `note_versions` 保存长期资料。每条笔记包含稳定 ID、标题、正文、版本、归档标记和 1–32 个本会话原始 `seq`。引用存在不表示内容已被证实，笔记不授予权限。

上下文默认保留约 96 KiB 的近期历史，并保护全部未读入站与完整原生工具调用/结果组。请求整体仍限制为 512 KiB；若未读材料本身超限则明确停止，不悄悄推进检查点或删除历史。`history_search(query, after_seq, limit)` 检索保留的入站、媒体观察和真实动作结果，剥离传输密钥与私有审查；`next_after_seq` 用于翻页。不是向量库，也没有额外的自动摘要模型。

每轮只注入笔记标题、版本、摘要及依据索引。模型可使用 `notes_list`、`note_read`、`note_upsert`；新建版本为 0，修改携带当前版本。WebUI“笔记与技能”支持手动维护、归档和历史检索。检索出的 `seq` 可填入笔记依据；不复制日志中的签名下载链接。

## SKILL 包

运行配置 `skills_dir` 默认为仓库 `skills`。HZ 使用独立可编辑目录 `/opt/mutsumi-sync-v4/shared/skills`，发布不覆盖用户编辑。

```text
skills/
  example/
    SKILL.md
    references/check.md
```

```markdown
---
name: example
description: 适用场景与用途
---

具体方法，以及如何核对结果。
```

目录只注入名称、描述、资源路径与内容哈希 revision。先调用 `skill_read(name, "SKILL.md", revision)`，再按需读取 UTF-8 文本资源；资源或目录变化会改变 revision。WebUI 提供包、路径、文本编辑，保存前验证整个候选包，版本冲突保留草稿。无效外部编辑保留进程内上一份有效快照并显示警告；首次启动目录无效时不加载技能。

最多 64 包、每包 64 文件/256 KiB、单文件 64 KiB、目录深度 4；拒绝符号链接和越界路径。脚本文本可以作为资料读取，加载不会自动执行，也不会新增宿主能力。需要执行时仍调用真实的 bash/edit 等工具，受原有隔离与 dry-run 约束。内置 `evidence-notes` 与 `external-reports` 可直接参考。

## 内部投递

`deliveries` 保存 attention 唤醒和 vision 观察任务。入站与观察任务同事务提交，原始事件 ID 去重；完成时用 generation + lease 比较，旧工作不能删除后来到达的新消息。媒体观察完成事件与 attention 唤醒同事务提交。重启恢复 running 为 pending；内部错误有限退避，视觉连续失败三次留下明确 unavailable 观察。

通知原语只用于加速唤醒，SQLite 才是投递事实源。WebUI 展示队列数量和尝试次数。动作账本继续区分 not_started、failed 与 unknown；QQ 外发不会因为内部队列恢复而自动重试。此处实现已有图片处理与注意力投递，没有宣称增加后台子代理执行器或定时任务系统。

## 官方 QQ 与 NapCat 并存

Rust 适配器 `src/qqbot.rs` 复用 Runtime、Reviewer、Executor、Ledger 和媒体库。独立 Node 进程 `adapters/qqbot` 使用锁定的腾讯 `@tencent-connect/qqbot-nodejs` 1.0.5，仅负责平台连接、鉴权和媒体协议，不运行第二个 Agent。

`server.yaml` 默认 `qq_official: null`。配置示例在 `config/server.example.yaml`。开启配置后，每次 Rust 重启仍默认关闭；WebUI“连接与登录 → 官方 QQ Bot”需确认桥接已连接才能开启。NapCat 配置与开关保持独立。

身份由 app_id、scope、conversation OpenID、actor OpenID 显式绑定；C2C 使用 user_openid，群使用 group_openid/member_openid。不能把现有数字 QQ、昵称或正文自称映射为主人。第一版仅接入配置中列出的会话与发送者，新增绑定需修改私有配置后重启，现有 NapCat 角色页不代管 OpenID。

桥接只监听 `127.0.0.1:18241`，用独立随机 bearer key 调用 Rust `/qqbot/events`；Rust 使用同 key 访问桥接。此密钥不是 WebUI 口令、模型 key 或 AppSecret。桥接仅收到自身 AppSecret，无权读模型配置。

```sh
cd adapters/qqbot
npm ci --omit=dev --ignore-scripts
# 根据 bridge.env.example 设置私有环境，不将实际值提交 Git。
npm start
```

HZ 单元模板 `deploy/hz/mutsumi-qqbot.service` 采用独立用户、私有状态目录；没有真实凭据时保持 disabled/inactive。密钥文件为 root:root 0600；`MUTSUMI_QQBOT_BRIDGE_KEY` 同时添加到 Rust 的私有 service.env。不要把下载签名或平台凭据写进聊天或普通事件。

入站先在桥接日志落盘，再向 Rust 投递并重试至受理；Rust 对相同平台事件去重。忽略未启用或未绑定来源是明确受理结果，不在后来开启时补发。桥接保留最近七天原始附件定位信息，模型只接收位置与受控媒体引用。SDK 未提供文字和附件精确交错位置，适配器保留文本与附件顺序，不声称恢复未知布局。

发送保持 messages/segments：纯文字用文本接口，独立图片通过 SDK 上传且 `srvSendMsg:false`，再单独提交发送；混排用 Markdown + 平台热素材引用，原始图片/GIF 字节不重渲染。热素材缓存与本地原图库分离，遵守平台 100 张配额，混排单图限制 10 MiB；缓存仅淘汰本桥接管理的素材。原生 face 暂不支持，使用 Unicode emoji 或真实媒体引用。

被动回复保守取五分钟和次数限制，机会来自已提交原始入站；超时不擅自降级主动发送。`allow_proactive` 必须在目标配置中明确开启，仍受平台权限/频控限制。桥接 outbox 在发送前写入 started，保存平台真实 message_id；同动作同参数返回保存回执，冲突拒绝。started/unknown 在重启或断线后均不自动重放。

真实平台验收仍需 Bot AppID/AppSecret、测试用户/群 OpenID 与手机 QQ 核验；离线测试证明 HTTP/持久化/协议边界，不证明平台权限、客户端 Markdown 排版或 GIF 动画表现。NapCat 继续保留。

## 验证入口

Rust：`cargo test --locked --all-targets`、`cargo clippy --locked --all-targets -- -D warnings`、`cargo fmt --all -- --check`。

桥接：`cd adapters/qqbot && npm ci --ignore-scripts && npm test`。测试真实 HTTP、入站重启恢复、重复消息、未知发送不重试、原 GIF 字节和混排素材缓存。Rust 集成测试覆盖独立开关、身份绑定、媒体入队与知识管理接口。

平台协议依据：[腾讯官方 SDK](https://github.com/tencent-connect/qqbot-nodejs)、[素材管理](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/material-management.html)。此前工程调研见 [生态调研](ecosystem-research-2026-11-02.md)。
