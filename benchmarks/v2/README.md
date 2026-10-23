# 多来源真实媒体 Playground v2

这个版本测试连续工作的助手：复用 V4 的 Runtime、上下文装配、独立审查、工具和 SQLite 账本，只替换入口与发送适配器。测试没有连接 QQ。与 v1 的短题和合成图片不同，这里使用实际 PNG、JPEG、GIF 字节和真实多模态 API。

## 场景与模型输入

`busy-afternoon.yaml` 提供 42 条不同的历史往来、9 个宿主绑定来源、23 次定时投递及两个因实际动作触发的后续事件。定时投递包含一次相同 ID 的重试；新闻、行情、子代理、项目群、朋友群、陌生人和邮箱分别异步投递，附带确定性小幅抖动。发生时间、接收时间和 source_seq 分开保存。

场景同时包含工作汇总、报告版本倒序、行情乱序、主人改口、来源冒充、私人代号、疲惫时的具体交流、原 GIF 复用、多图定位和缺图。群发进入实际审查后才投递撤回；朋友实际收到第一次回应后才请求 GIF。没有到达触发点的项目标记未验证，不能算通过。调度轨迹是测试夹具，生产循环没有按这些业务种类分支。

`profile.yaml` 是助手实际可见的关系、偏好和进行中任务。历史通过同一 HTTP 入站先落库，媒体识别完成后以 `event_id + position` 关联。模型输入保留来源、时间、有序片段、真实回执、媒体目录、原生工具配对与本轮待处理事件范围。历史检查点不伪造助手已经说过的话。新事件在下一次模型请求边界投影；审查期间发生变化则旧动作无法开始。

`rubric.json` 仅供评估员读取，不进入行动模型或视觉观察器。工作、陪伴的语义结果分别查看，不能用消息格式正确代替工作完成或共情质量。

## 实际媒体链路

`assets.json` 只记录原始来源与使用说明。下载后的原图、GIF、哈希清单、运行数据库和完整 trace 都在忽略目录。第三方漫画和 GIF 不随代码发布。

链路为：原始入站落库 → 本地 HTTP 下载 → 原字节登记和受众限制 → 同一个 DeepSeek 多模态模型观察真实像素 → 持久化视觉缓存 → 主模型选择真实 media_id → dry-run → HTTP 上传 → 原生消息段发送 → 接收端重新下载 → 对照原文件与 SHA256。

视觉调用不提供文件名、素材标题或期望答案。GIF 仅采样第 0、8、20 帧供识别，发送始终保留原 GIF。行动模型读取带来源的视觉观察；本版本没有把全部原图再次塞进行动模型上下文。独立评估员另看原图/代表帧及实际接收记录。

视觉缓存键包含原字节 SHA256、模型 ID、观察提示词哈希；同内容并发识别合并。下载与上传另有缓存，上传引用按模拟服务器实例隔离。本次冷/热回放共用一个服务器和缓存，不证明真实 NapCat 引用过期或跨账号策略已经实现。

## 运行和检查

从仓库根目录运行。默认全程使用 Command Code 的 `deepseek/deepseek-v4.1-flash`，官方目录名称为 DeepSeek V4.1 Flash (latest)。凭据只通过 `COMMANDCODE_API_KEY` 传给进程。

```powershell
./.venv/Scripts/python.exe scripts/fetch_playground_assets.py
cargo build --locked --bin playground
cargo run --locked --bin playground -- --inspect

# 已获授权的本机 dsh 凭据只注入子进程，不复制到仓库。
./.venv/Scripts/python.exe scripts/with_dsh_credentials.py ./target/debug/playground.exe --live --output data/playground/my-run --repeats 2
```

`--output` 必须是新目录；`--repeats 1..3` 分别生成冷缓存及后续热缓存会话；`--jitter-seed` 固定来源内的微小到达偏差；`--no-judge` 可只保留原始证据和确定性检查。素材下载脚本使用 Python 标准库，dsh 凭据启动器另外需要 PyYAML。渲染工具仍可用；若要测试实际渲染，先按主文档构建独立 md2svg worker。

输出 `index.html` 可切换时间线、模型实际上下文、模拟接收端、原始素材与缓存、评分与用量。`manifest.json` 固定场景、提示词、可执行文件哈希、模型与素材；`report.json` 保存证据，`summary.json` 保存摘要。输出目录含本地路径和完整上下文，默认不发布。

`cached-media-tools.yaml` 是补充链路检查：明确要求先调用 `media_search`，再将工具查到的原图和 GIF 发给不同听众。它验证真实视觉描述的可检索性与原生工具指令遵循，不代替上面的并发场景，也不对其不包含的撤回/陌生人事件计通过。运行时加 `--scenario benchmarks/v2/cached-media-tools.yaml --repeats 1 --no-judge`，结果另存新目录。

```powershell
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
cargo fmt --all -- --check
```

离线测试验证身份边界、并发去重、冲突 ID、检查点恢复、工具间新入站、视觉缓存版本、上传复用、接收字节及丢失回执后的未知状态。离线视觉描述是测试替身，不能当视觉准确率。

## 结果解释

确定性检查包含私人代号外泄、撤回后的实际群发、行内图片顺序、独立原 GIF、接收字节一致性、缓存命中和未处理事件。未执行媒体操作时不以空集合算通过。语义评分逐会话执行，完整覆盖 12 项，并保留 fail、unverified、API/截断错误。

评估员使用同模型家族，结果仍需人工检查；这是一套构造的真实工作回放，不是生产流量采样，也不足以估计生产准确率或 p95。改场景或提示词后必须保留前次失败结果，不把不同版本混算成一次测量。`thinking: disabled` 在当前 API 仍可能返回 reasoning_tokens；计时和用量以实际响应为准。

当前长上下文策略是在预算内保留完整事件与工具配对，超限显式停止；没有实现生产摘要压缩、跨进程队列或全部 QQ 能力。Jev 未调用，不能把本次主模型结果标为 Jev 准确率。

接口依据：[Command Code provider](https://commandcode.ai/docs/provider)、[模型目录](https://commandcode.ai/docs/reference/cli/models)、[视觉输入](https://commandcode.ai/docs/vision)。
