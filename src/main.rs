use anyhow::Result;
use clap::Parser;
use mutsumi_runtime::{app, config::Config, runtime::Input};
use std::path::PathBuf;

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
    let runtime = app::build(config).await?;
    let signal_cancel = runtime.executor.cancel.clone();
    let signal = tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            signal_cancel.cancel();
        }
    });
    let started = std::time::Instant::now();
    let outcome = runtime
        .run(Input {
            session: args.session,
            source_target: args.source,
            actor: args.actor,
            text: args.text,
            segments: vec![],
        })
        .await;
    signal.abort();
    let _ = signal.await;
    if let Some(media) = &runtime.executor.tools.media {
        media.worker.shutdown().await?;
    }
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
