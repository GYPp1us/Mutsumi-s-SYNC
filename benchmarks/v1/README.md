# 工作与陪伴 benchmark v1

这是合成 pilot：24 个路由基础用例、16 个媒体基础用例。标签由 Agent 按已确认的契约编写，尚未经独立人工复核。不能据此声称生产准确率或情感陪伴质量。

## 输入与隔离

- `route_cases.json`：工作和陪伴各 12 例，包括简单问候、问候附带任务、依赖上文的确认、改口、引用注入、其他会话干扰、具体情绪困难和明确静默。
- `media_cases.json`：两维度各 8 例，包括多图选择、行内编排、分条、跨会话、真实 Markdown 渲染、GIF 原样复用、私密图片、未知素材和描述失败。
- `quick-replies.yaml`：可编辑的早安、晚安和通用表情接龙。模板描述直接形成 Jev choice criteria，不维护另一套分类分支。
- gold、工具断言和期望消息只进入评分器；模型只收到请求、合成历史、真实素材资料和工具 schema。
- `compact` 使用相关历史；`long-front/middle/end` 加入同一段 8,000 个 Unicode 字符的无关归档，改变相关历史的位置。字符数不是 token 数。同一基础用例的变体相关，不能当成独立样本扩大显著性。
- 媒体均为程序生成的 PNG/GIF，描述由 fixture 提供。本版本不测视觉识别。入站使用原生数组归一化，多图与未知图片的位置均保留。

## 运行

从仓库根目录运行，API key 只放在当前进程环境。结果与合成原始 trace 写入被忽略的 `data/benchmarks/`，不应提交真实用户数据。

```powershell
cargo run --locked --bin benchmark -- inspect
cargo build --locked --manifest-path workers/md2svg/Cargo.toml -j 2

# 设置 COMMANDCODE_API_KEY；实际使用主模型与独立模型审查，会产生费用。
cargo run --locked --bin benchmark -- --live --profiles compact,long-middle media

# 设置 TYPESAFE_API_KEY。默认固定 jev-1.13.0，不使用浮动别名。
cargo run --locked --bin benchmark -- --live --profiles compact,long-front,long-middle,long-end jev
# 明确比较 preview，不将两种模型结果混成一个准确率。
cargo run --locked --bin benchmark -- --live jev --model jev-1.14.0

# 不调用 API 的真实 worker 生命周期测试
$env:MUTSUMI_RENDER_WORKER = (Resolve-Path 'workers/md2svg/target/debug/mutsumi-md-worker.exe').Path
cargo test --locked --test media_chain -- --ignored --nocapture
```

`--output` 可指定独立结果目录，`--limit` 适合先做冒烟。媒体命令的 `--worker` 可指定其他平台的可执行文件路径。缺少 Jev key 表示未运行，不是准确率为零；不得以其他模型代跑后标记为 Jev。

## 评分与结果

路由记录原始 choice 与阈值后的实际路线，按维度、上下文、calibration/test 分别汇总。重点关注必须生成的请求被误判为模板或静默的次数，同时报告有效响应、传输/格式错误、模板覆盖与延迟。每题保留 Jev 实际返回的 model、usage、probabilities 和 confidence；Noul 另外计算逐题 Brier 分数。confidence 和所选概率是两项不同数值。

初始阈值预先固定为 probability ≥ 0.95、confidence ≥ 0.80，尚无校准结论。若后续调阈值，只使用 calibration 分组；同一基础用例的所有长短变体必须留在同一分组。test 分组不参与选阈值。暂未验证选项次序稳健性。

媒体分别记录首个非空消息批次是否遵循、门禁拒绝次数、最终实际消息与工具结果。精确用例比较目标、消息边界、段顺序与真实媒体 ID；纯文字用例只检查结构和工具限制，不自动评价共情、诚实或措辞质量。`end_to_end_pass` 还要求本轮完成、必要工具实际成功、未提议禁用工具。所有有效输出是本地 OneBot outbox，不是 QQ 送达。

真实渲染另外检查成功工具回执和已登记媒体；图片内容是否忠实于原文仍需抽样人工查看。渲染 worker 复用 Renderer，超时后 kill + wait；不支持的 HTML 不生成替代图片。所有计时包含模型、独立审查、纠正和本地 I/O；首个请求的冷启动不能解释为稳定延迟。一次测量不足以估计生产 p95。

情感维度建议人工另评：是否遵守“不发图/不空泛鼓励”、是否理解具体处境、是否自然且不过度承诺；这些分数不与结构通过率合并。

接口依据：[System One](https://docs.typesafe.ai/api/system-one)、[模型版本](https://docs.typesafe.ai/models)、[已知能力边界](https://docs.typesafe.ai/model-jaggedness/jev-1.13)、[md2svg 固定提交](https://github.com/GYPp1us/md2svg/tree/91cf197ea5fb082dceeb09abc746712c9b670eca)。
