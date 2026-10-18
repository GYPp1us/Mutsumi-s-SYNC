use anyhow::Result;
use clap::Parser;
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
use std::{path::PathBuf, sync::Arc};
use tokio_util::sync::CancellationToken;

#[derive(Parser)]
#[command(about = "V4 Rust orchestration prototype: terminal transport, dedicated SQLite ledger")]
struct Args {
    #[arg(long, default_value = "config/v4.example.yaml")]
    config: PathBuf,
    #[arg(long, default_value = "local-test")]
    session: String,
    #[arg(long, default_value = "owner_private")]
    source: String,
    #[arg(long, default_value = "local_owner")]
    actor: String,
    #[arg(long)]
    text: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let config = Config::load(&args.config)?;
    let prompts = config.prompts()?;
    let model = Arc::new(OpenAiCompatible::new(config.provider.clone())?);
    let ledger = Ledger::open(std::path::Path::new(&config.database)).await?;
    let cancel = CancellationToken::new();
    let signal_cancel = cancel.clone();
    let signal = tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            signal_cancel.cancel()
        }
    });
    let executor = Executor {
        ledger,
        reviewer: Arc::new(ModelReviewer {
            model: model.clone(),
            system: prompts.reviewer.clone(),
        }),
        sender: Arc::new(TerminalSender(config.directory.clone())),
        directory: config.directory.clone(),
        tools: ToolRegistry,
        cancel,
    };
    let runtime = Runtime {
        quick_replies: QuickReplies::load(std::path::Path::new(&config.quick_replies))?,
        config,
        prompts,
        model,
        executor,
        router: Arc::new(MainModelRouter),
    };
    let started = std::time::Instant::now();
    let outcome = runtime
        .run(Input {
            session: args.session,
            source_target: args.source,
            actor: args.actor,
            text: args.text,
        })
        .await;
    signal.abort();
    let _ = signal.await;
    let outcome = outcome?;
    eprintln!(
        "{}",
        serde_json::json!({"outcome":outcome,"elapsed_ms":started.elapsed().as_millis()})
    );
    if !matches!(outcome.status.as_str(), "completed" | "silent") {
        std::process::exit(2)
    }
    Ok(())
}
