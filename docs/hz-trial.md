# HZ V4 试用服务

V4 的网页、外部上报和 NapCat 适配器共用 `Runtime`、`Scheduler::step`、SQLite 账本和独立动作审查。入口接收后先落盘，模型只在下一次 provider 请求前读取新事件；工具执行期间 HTTP 入口继续接收消息。

## 使用

入口为 `https://hz.arcol.site/sync/`。访问口令保存在 HZ 的 `/opt/mutsumi-sync-v4/shared/access.txt`，只允许 root 读取。带 `#access=` 的启动链接会在网页读取后立即从地址栏移除，片段不会发到服务器访问日志。登录会话有效期 12 小时，重启后重新登录。

- **聊天**：输入文字，或上传 PNG、JPEG、GIF。图片原件保留，可选择授权 QQ 复用；默认只用于网页。模型提议使用 `messages[].segments[]`，网页只展示实际入账的消息。
- **QQ 连接**：显示 NapCat 登录二维码，扫码后再打开“允许 QQ 收发”。每次服务重启均关闭 QQ 收发。目前接收范围仅限配置中主人的 QQ 私聊；群聊、陌生人接入仍待实现。关闭期间的消息不排队供之后自动补答。
- **外部上报**：显示新闻、行情、子代理、邮件四个来源的独立地址、口令和可复制示例。外部口令与网页访问口令相互独立。
- **事件与工具**：查看实际收发、工具结果、拒绝和未知状态。模型或工具异常会暂停自动推进；主人发来新的指令后才恢复，不由外部上报自动解除。

网页回执 `webui_store` 表示保存到网页消息流；NapCat 回执 `napcat_onebot` 表示 OneBot 接口返回消息 ID。两者均不证明人类已经阅读。

## 外部上报协议

`POST /sync/hooks/{news|market|agent|mail}`，请求头 `Authorization: Bearer <该来源口令>`：

```json
{
  "id": "market-unique-001",
  "occurred_at": "2026-10-24T12:00:00+08:00",
  "text": "模拟行情：指数较开盘上涨 1.2%，请自行判断是否值得提醒主人。",
  "media": [],
  "correlation_id": "optional-task-id"
}
```

同一来源的相同 `id` 与内容重试返回 `duplicate: true`；同 ID 的不同内容被拒绝。身份、关系和会话由服务配置绑定，请求正文不能指定或提升身份。`media` 只能引用已登记且具有相应受众权限的媒体 ID，不提供任意网址下载。

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

代码入口：`src/bin/server.rs`、`src/web/{mod,routes}.rs`；网页为 `assets/trial.{html,js}`，NapCat 为 `src/napcat.rs`，工具为 `src/workspace.rs`。提示词继续集中在 `system-prompts.yaml`，人设在私有 `profile.yaml`；修改后重启服务生效。

`deploy/hz/prepare.py` 是针对现有 HZ 布局的准备脚本，需要 root 和 JSON 标准输入。只有用户明确授权传输凭据后，才可提供 `commandcode_key`；脚本不打印凭据、不启动服务。它备份原 NapCat 网络配置，设置两个不同的 OneBot token，绑定接口到回环地址，停用旧 NapCat 定时启停，校验 nginx 配置。V3 服务和归档不恢复、不修改。

Rust 服务限制可写目录、内存和进程数量；Markdown 使用独立 md2svg/resvg worker。构建使用锁文件。启动前安装 bubblewrap、字体和构建依赖，并分别构建主程序与 `workers/md2svg`，将二进制放入发布的 `bin/`。

`python deploy/hz/source_bundle.py data/hz-source.tar.gz` 可生成只含 Git 跟踪的 V4 构建输入包，自动包括 SQL、HTML 等内嵌资源。构建主程序使用 `cargo build --release --locked --bin server`；worker 在 `workers/md2svg` 中单独执行 `cargo build --release --locked -j 1`，需要 Rust 1.92 或更新版本。HZ 为小内存机器，首次构建较慢，后续应复用 Cargo 缓存。系统依赖包括 `build-essential pkg-config libssl-dev bubblewrap fonts-noto-cjk python3-yaml`；现有原生 NapCat 另依赖 xvfb。

此版本供主人试用，尚不覆盖完整 QQ 群聊、陌生人、跨进程队列与生产级上下文压缩。真实 QQ 发送必须在扫码登录并开启收发后验证，不能用本地 outbox 或接口在线代替送达测试。
