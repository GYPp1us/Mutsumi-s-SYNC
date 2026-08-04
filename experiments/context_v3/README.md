# Context v3 实验

这是一组不做上下文回收的对照实验，用来先分离三个问题：

- 模型能否召回明确事实；
- 模型能否遵守角色和隐私边界；
- 模型能否正确完成 `delegate_task`、`scheduler` 和带明确 `target` 的 `send`。

三种布局使用同一组 20 条资料和 9 个请求：

- `ledger_projection`：当前 Event Ledger 的 audience-filtered 投影；
- `task_room_usc`：共享事项记忆加当前 actor 的 private lens；
- `formatted_llm`：把所有角色记录放入一个格式化窗口，只依赖提示词和 `[user]` 协议。

## 运行

只生成提示和 token 规模，不联网：

```powershell
python -m experiments.context_v3.run --dry-run
```

真实模型运行前，在当前进程设置 key。优先尝试 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY = "..."
python -m experiments.context_v3.run --smoke
Remove-Item Env:DEEPSEEK_API_KEY
```

没有 DeepSeek key 时，客户端会尝试 `HUNIUHUB_API_KEY`、`OPENAI_API_KEY` 或 `MUTSUMI_EXPERIMENT_API_KEY`，并使用 `gpt-5.6-luna` 的 Responses 协议。API key 不从 `config.yaml`、`.env` 或源码读取，也不会写入结果。

完整实验：

```powershell
python -m experiments.context_v3.run --out experiments/context_v3/results/live.json
```

`--smoke` 覆盖事实召回、隐私边界、Worker 委派和群投递；全量包含 9 个用例。结果中的 `provider.api_key_env` 只有环境变量名，没有 key。

## 解释结果

重点看每个 design 的 `category_rates`：

- `fact_recall`：基础事实是否能找回；
- `boundary`：是否拒答且不泄露；
- `instruction_following`：普通输出、委派和定时注册；
- `routing`：群题目、私聊答案和群内答案请求是否准确寻址。

这轮的 `context_tokens` 只表示完整输入规模，不代表压缩成本。回收和长上下文稳定性继续引用 `docs/experiments/context-v2-comparison.md`，不会和这轮结论混在一起。

## 长事项实验

更复杂的 Project Atlas 场景包含 320 条噪声记录、旧/新决策、Alice/Bob/Carol 三条私聊、群聊、两次 Worker 回报、定时注册、群与对应私聊投递、fan-out，以及一个运行时才出现的新合作方关系。Baseline 使用真实的 `MessageStore + build_life_stream`，另外两种设计使用同一批资料的 Task Room 或全角色格式化窗口。

```powershell
$env:DEEPSEEK_API_KEY = "..."
python -m experiments.context_v3.long_run --turns 320 --max-tokens 2400 --out experiments/context_v3/results/live.json
Remove-Item Env:DEEPSEEK_API_KEY
```

本次实际运行的脱敏结果在 `experiments/context_v3/results/deepseek_full_regraded.json`，可视化报告在 `docs/experiments/context-v3-results.html`。本次使用 DeepSeek V4 Flash；只有 DeepSeek 不可用时才走 README 前面的 Luna Responses fallback。
