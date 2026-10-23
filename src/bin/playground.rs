use anyhow::{Result, bail};
use clap::Parser;
use mutsumi_runtime::playground::{self, Options};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    about = "Concurrent multi-source replay using the normal V4 runtime and loopback media simulator"
)]
struct Args {
    #[arg(long, default_value = "config/playground.example.yaml")]
    config: PathBuf,
    #[arg(long, default_value = "benchmarks/v2/busy-afternoon.yaml")]
    scenario: PathBuf,
    #[arg(long, default_value = "data/playground/assets")]
    assets: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value_t = 2)]
    repeats: usize,
    #[arg(long, default_value_t = 23)]
    jitter_seed: u64,
    #[arg(long)]
    live: bool,
    #[arg(long)]
    no_judge: bool,
    #[arg(long)]
    inspect: bool,
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if args.inspect {
        println!(
            "{}",
            serde_json::to_string_pretty(&playground::inspect(&args.scenario)?)?
        );
        return Ok(());
    }
    if !args.live {
        bail!("real model and vision calls require --live")
    }
    if !(1..=3).contains(&args.repeats) {
        bail!("repeats must be 1..3")
    }
    let output = args.output.unwrap_or_else(|| {
        PathBuf::from(format!(
            "data/playground/run-{}",
            uuid::Uuid::new_v4().simple()
        ))
    });
    let result = playground::run(Options {
        config: args.config,
        scenario: args.scenario,
        assets: args.assets,
        output: output.clone(),
        repeats: args.repeats,
        judge: !args.no_judge,
        jitter_seed: args.jitter_seed,
    })
    .await?;
    println!(
        "{}\nReport: {}",
        serde_json::to_string_pretty(&result)?,
        output.join("index.html").display()
    );
    Ok(())
}
