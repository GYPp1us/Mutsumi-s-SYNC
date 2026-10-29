# HZ V4 试用服务

V4 的网页、外部上报和 NapCat 适配器共用 `Runtime`、`Scheduler::step`、SQLite 账本和独立动作审查。入口接收后先落盘，模型只在下一次 provider 请求前读取新事件；工具执行期间 HTTP 入口继续接收消息。

## 使用

入口为 `https://hz.arcol.site/sync/`。访问口令保存在 HZ 的 `/opt/mutsumi-sync-v4/shared/access.txt`，只允许 root 读取。带 `#access=` 的启动链接会在网页读取后立即从地址栏移除，片段不会发到服务器访问日志。登录会话有效期 12 小时，重启后重新登录。

- **聊天**：输入文字，或上传 PNG、JPEG、GIF。图片原件保留，可选择授权 QQ 复用；默认只用于网页。模型提议使用 `messages[].segments[]`，网页只展示实际入账的消息。
- **QQ 连接**：显示 NapCat 登录二维码，扫码后再打开“允许 QQ 收发”。每次服务重启均关闭 QQ 收发。默认接入主人私聊；可在角色管理中显式启用已登记联系人。群聊尚未接入。关闭期间的消息不排队供之后自动补答。
- **角色管理**：按 QQ 号设置主人、朋友、陌生人、屏蔽及启用状态，并选择“仅主人”或“已登记且启用的联系人”。必须且只能保留一个已启用的主人，未登记账号不会自动升级身份。“最近收到的 QQ 消息”展示接收时角色、账号和未接入原因，可直接登记新账号。保存即时生效并持久化；正在处理消息时需稍后保存，历史入站身份不改写。
- **模型配置**：编辑服务商链接、API基址、可选对话端点、模型名、Key、超时、输出上限和高级JSON选项；可先测试连接再保存。主模型、审查与视觉同步切换，下一轮生效，无需重启。Key不回显、不进入模型上下文和事件。
- **外部上报**：显示模型已注册的事件、独立口令和统一地址的可复制请求示例。先在聊天中要求助手注册所需事件；不再预设新闻、行情等固定来源。
- **事件与工具**：查看实际收发、工具结果、拒绝和未知状态。模型或工具异常会暂停自动推进；主人发来新的指令后才恢复，不由外部上报自动解除。

网页回执 `webui_store` 表示保存到网页消息流；NapCat 回执 `napcat_onebot` 表示 OneBot 接口返回消息 ID。两者均不证明人类已经阅读。

## 外部上报协议

先让模型实际调用 `register_event`，例如注册 `market.alert`、说明其用途并选择 `source_kind: service`。登记和启停经过统一 dry-run；定义不包含自动工作流，模型仍自行判断收到报告后该做什么。

所有事件共用 `POST /sync/hooks/events`，请求头为 `Authorization: Bearer <该事件口令>`：

```json
{
  "event": "market.alert",
  "id": "market-unique-001",
  "content": {"change_percent": 1.2, "message": "模拟行情观察"},
  "occurred_at": "2026-10-29T12:00:00+08:00",
  "correlation_id": "optional-task-id"
}
```

`event`、`id`、`content` 必填，后两项时间/关联字段可省略。content 可以是字符串或结构化 JSON。同一事件的相同 ID 与请求体重试返回 `duplicate: true`，同 ID 的不同内容被拒绝；未提供时间也能稳定去重。

事件名、凭据、来源类型和启用状态由注册表绑定。正文不能指定或提升身份，注册来源只能为 service、email、child_agent，关系统一为 registered_report。注册表凭据只通过已登录的管理页提供，注册工具和模型目录均不返回口令。`set_event_enabled` 停用后拒绝新上报；校验与入账在同一个事务中完成。旧 `/hooks/news`、`/hooks/mail` 等独立地址已移除，旧静态 hooks 配置仅兼容读取，不再产生接入能力。

NapCat 的 `/onebot/events` 使用 `X-Signature: sha1=<HMAC-SHA1十六进制值>`，签名对象为原始 HTTP body，密钥为独立 OneBot 上报 token；不能用 Bearer 头替代。所有通过鉴权且账号匹配的回调先落原始账本，再记录接入决定；未接入消息不会进入模型注意力队列。角色设置保存在 SQLite 的宿主 profile 中，`server.yaml` 的 `owner_id` 仅用于首次迁移。普通消息不具备修改角色的能力。

目标引用绑定稳定 QQ 账号。原来的 `qq_owner` 仍指向原账号，新增账号使用 `qq_private_<QQ号>`；更换主人不会把旧媒体受众授权转给新账号。角色变更只在两个模型处理回合之间提交，同时更新宿主事实使旧审查失效。配置决定哪些私聊可以接入，是否回复、怎样回复和工具选择仍由模型决定，并经过统一 dry-run。

## 模型配置与私有存储

API 基址留在原服务商时，Key 留空表示保留；更换服务地址时必须填写新的 Key，避免把当前凭据意外送到其他服务。对话端点留空则使用 API基址 + `/chat/completions`，也可指定同一服务地址下的完整URL或绝对路径。目前支持 OpenAI 兼容的 messages/tools 对话协议。

连接测试发起一次最多256输出token的小型真实请求，不修改当前配置。保存时若正在处理消息则返回冲突；一轮主模型与审查使用同一配置快照。视觉缓存含模型、端点与选项的配置指纹，重新识图时不会命中其他配置的缓存；已有媒体的历史观察仍会保留。

配置与 Key 保存在权限受限的运行数据库 `private_settings` 表；宿主 profile 和事件只保存配置版本及模型名。私有设置与宿主事实在同一事务中提交，环境变量仅作为首次默认值。不要将运行数据库、备份或 Key 提交到仓库。数据库已升级为版本4，保留版本1–3的入站与动作历史。

## 基础工具

工具 schema 是模型能力的事实源；全部通过现有统一审查和动作生命周期。

| 工具 | 作用与范围 |
| --- | --- |
| `bash` | Linux bubblewrap 独立命名空间；只写 `/workspace`，无网络、无模型 key、无宿主业务目录，`/proc` 为空（依赖 procfs 的工具不可用）。1–120 秒，stdout/stderr 各保留 16 KiB，退出后回收子进程。超时/取消保留已发生效果，标记未知，避免自动重放。 |
| `edit` | 工作目录相对路径的 UTF-8 文件精确替换；`old_text` 必须唯一，空串只用于新建。拒绝目录逃逸和符号链接，文件上限 512 KiB。 |
| `wait` | 异步等待 1–120 秒，随后重新读取上下文。不等于后台定时任务。 |

另有 clock、media_search、render_markdown。模型为 Command Code 的 `deepseek/deepseek-v4.1-flash`；Jev 尚无真实凭据，试用服务直接使用生成模型。

## 部署与编辑入口

| 内容 | HZ 路径 |
| --- | --- |
| 当前发布 | `/opt/mutsumi-sync-v4/current`，指向 `releases/` 的具体版本 |
| 运行配置 | `/opt/mutsumi-sync-v4/shared/{server,runtime,profile}.yaml` |
| 密钥环境 | `/opt/mutsumi-sync-v4/shared/service.env`，root:root 0600 |
| 工作目录 | `/opt/mutsumi-sync-v4/shared/workspace` |
| SQLite / 媒体 | `/var/lib/mutsumi-v4/` |
| Rust 服务 | `mutsumi-sync-v4.service`，专用用户 `mutsumi-v4` |
| NapCat | `mutsumi-napcat.service`，沿用原安装与身份 |
| HTTPS 代理 | `/etc/nginx/snippets/mutsumi-v4-location.conf` |

代码入口：`src/bin/server.rs`、`src/web/{mod,routes}.rs`；网页为 `assets/trial.{html,js}`，NapCat 为 `src/napcat.rs`，工具为 `src/workspace.rs`。提示词集中在 `system-prompts.yaml`，默认使用无固定人设的通用协议；可选人设单独填入 `persona`，私有 `profile.yaml` 保存关系、偏好和工作资料。编辑说明见 [系统提示词](system-prompts.md)，修改后重启服务生效。

`deploy/hz/prepare.py` 是针对现有 HZ 布局的准备脚本，需要 root 和 JSON 标准输入。只有用户明确授权传输凭据后，才可提供 `commandcode_key`；脚本不打印凭据、不启动服务。它备份原 NapCat 网络配置，设置两个不同的 OneBot token，绑定接口到回环地址，停用旧 NapCat 定时启停，校验 nginx 配置。V3 服务和归档不恢复、不修改。

Rust 服务限制可写目录、内存和进程数量；Markdown 使用独立 md2svg/resvg worker。构建使用锁文件。启动前安装 bubblewrap、字体和构建依赖，并分别构建主程序与 `workers/md2svg`，将二进制放入发布的 `bin/`。

`python deploy/hz/source_bundle.py data/hz-source.tar.gz` 可生成只含 Git 跟踪的 V4 构建输入包，自动包括 SQL、HTML 等内嵌资源。构建主程序使用 `cargo build --release --locked --bin server`；worker 在 `workers/md2svg` 中单独执行 `cargo build --release --locked -j 1`，需要 Rust 1.92 或更新版本。HZ 为小内存机器，首次构建较慢，后续应复用 Cargo 缓存。系统依赖包括 `build-essential pkg-config libssl-dev bubblewrap fonts-noto-cjk python3-yaml`；现有原生 NapCat 另依赖 xvfb。

此版本供主人试用，尚不覆盖 QQ 群聊、未登记陌生人自动接入、跨进程队列与生产级上下文压缩。真实 QQ 发送必须在扫码登录并开启收发后验证，不能用本地 outbox 或接口在线代替送达测试。
