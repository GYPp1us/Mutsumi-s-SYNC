//! Opt-in, paid provider calls. Only synthetic inputs and terminal output are used.
use anyhow::Result;
use mutsumi_runtime::{
    config::Config,
    executor::Executor,
    ledger::Ledger,
    provider::OpenAiCompatible,
    review::ModelReviewer,
    routing::{MainModelRouter, QuickReplies},
    runtime::{Input, Runtime},
    sender::TerminalSender,
    tools::ToolRegistry,
};
use serde_json::json;
use std::{path::Path, sync::Arc};
use tokio_util::sync::CancellationToken;

#[tokio::test]
#[ignore = "requires explicit COMMANDCODE_API_KEY and makes real provider calls"]
async fn real_model_terminal_orchestration() -> Result<()> {
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let prompts = config.prompts()?;
    let model = Arc::new(OpenAiCompatible::new(config.provider.clone())?);
    let temp = tempfile::tempdir()?;
    let ledger = Ledger::open(&temp.path().join("live.sqlite3")).await?;
    let executor = Executor {
        ledger: ledger.clone(),
        reviewer: Arc::new(ModelReviewer {
            model: model.clone(),
            system: prompts.reviewer.clone(),
        }),
        sender: Arc::new(TerminalSender(config.directory.clone())),
        directory: config.directory.clone(),
        tools: ToolRegistry::default(),
        cancel: CancellationToken::new(),
    };
    let runtime = Runtime {
        quick_replies: QuickReplies::load(Path::new("quick-replies.yaml"))?,
        config,
        prompts,
        model,
        executor,
        router: Arc::new(MainModelRouter),
    };
    let scenarios = [
        ("greeting", "greeting", "早安，简单回复一句就好。", false),
        (
            "tool_content",
            "clock",
            "请查询现在时间。先在同一条包含原生 tool_calls 的 assistant 消息的 content 中提出一条“我查一下时间”的私聊消息，然后调用 clock。拿到结果后简短回复具体时间。",
            true,
        ),
        (
            "cross_conversation",
            "cross",
            "这是本地终端路由验证，请向目录中的 project_group 发一条“测试完成”，不必给当前私聊再发消息。",
            false,
        ),
        (
            "history_reload",
            "clock",
            "上轮确实查询过时间吗？请根据已有工具回执简单回答，不用再次查询。",
            false,
        ),
    ];
    let mut report = vec![];
    for (id, session, text, needs_tool) in scenarios {
        let started = std::time::Instant::now();
        let result = runtime
            .run(Input {
                session: session.into(),
                source_target: "owner_private".into(),
                actor: "local_owner".into(),
                text: text.into(),
                segments: vec![],
            })
            .await?;
        report.push(
            json!({"scenario":id,"outcome":result,"elapsed_ms":started.elapsed().as_millis()}),
        );
        println!("{}", report.last().unwrap());
        if result.status != "completed"
            || result.sent_count == 0
            || (needs_tool && result.tool_count == 0)
        {
            let diagnostics = ledger.facts(session).await?;
            anyhow::bail!(
                "scenario {id} did not complete: {}; verified facts: {}",
                result.status,
                serde_json::to_string(&diagnostics)?
            );
        }
        if id == "tool_content" {
            let facts = ledger.facts(session).await?;
            let actions: Vec<_> = facts
                .iter()
                .filter(|e| e["kind"] == "action_result")
                .collect();
            assert_eq!(actions[0]["data"]["action"]["kind"], "send");
            assert!(
                actions
                    .iter()
                    .any(|e| e["data"]["action"]["kind"] == "tool")
            );
        }
        if id == "cross_conversation" {
            let facts = ledger.facts(session).await?;
            assert!(
                facts
                    .iter()
                    .any(|e| e["data"]["result"]["message"]["target_ref"] == "project_group")
            );
        }
    }
    std::fs::create_dir_all("data/v4-prototype")?;
    std::fs::write(
        "data/v4-prototype/live-smoke-report.json",
        serde_json::to_vec_pretty(
            &json!({"model":runtime.config.provider.model,"transport":"terminal","scenarios":report}),
        )?,
    )?;
    Ok(())
}
