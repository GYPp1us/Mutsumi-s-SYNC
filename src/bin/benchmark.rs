use anyhow::{Result, bail};
use clap::{Parser, Subcommand};
use mutsumi_runtime::benchmark::{self, write_json};
use serde_json::json;
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Synthetic work / companionship pilot; real calls require --live")]
struct Args {
    #[arg(long, default_value = "benchmarks/v1")]
    dataset: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, value_delimiter = ',', default_value = "compact,long-middle")]
    profiles: Vec<String>,
    #[arg(long)]
    limit: Option<usize>,
    #[arg(long)]
    live: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Inspect,
    Jev {
        #[arg(long)]
        model: Option<String>,
    },
    Media {
        #[arg(
            long,
            default_value = "workers/md2svg/target/debug/mutsumi-md-worker.exe"
        )]
        worker: PathBuf,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let dataset = benchmark::validate_dataset(&args.dataset)?;
    for profile in &args.profiles {
        benchmark::context("validation", profile)?;
    }
    if matches!(args.command, Command::Inspect) {
        println!("{}", serde_json::to_string_pretty(&dataset)?);
        return Ok(());
    }
    if !args.live {
        bail!("real API calls require the explicit --live flag")
    }
    let output = args.output.unwrap_or_else(|| {
        PathBuf::from(format!("data/benchmarks/{}", uuid::Uuid::new_v4().simple()))
    });
    if output.join("manifest.json").exists() {
        bail!("benchmark output already exists; choose a new directory to preserve counterexamples")
    }
    tokio::fs::create_dir_all(&output).await?;
    let kind = match &args.command {
        Command::Jev { .. } => "jev",
        Command::Media { .. } => "media",
        Command::Inspect => unreachable!(),
    };
    write_json(
        &output.join("manifest.json"),
        &json!({"kind":kind,"started_at":chrono::Utc::now(),"dataset":dataset,"profiles":args.profiles,"limit":args.limit,"system_prompts_sha256":benchmark::sha256(&std::fs::read("system-prompts.yaml")?),"jev_prompts_sha256":benchmark::sha256(&std::fs::read("jev-prompts.yaml")?),"runner_sha256":benchmark::sha256(&std::fs::read(std::env::current_exe()?)?),"md2svg_commit":"91cf197ea5fb082dceeb09abc746712c9b670eca","gold_in_model_context":false}),
    )?;
    let result = match args.command {
        Command::Jev { model } => {
            benchmark::jev::run(&args.dataset, &output, &args.profiles, args.limit, model).await
        }
        Command::Media { worker } => {
            benchmark::media::run(&args.dataset, &output, &args.profiles, args.limit, &worker).await
        }
        Command::Inspect => unreachable!(),
    };
    match result {
        Ok(report) => println!(
            "{}\nOutput: {}",
            serde_json::to_string_pretty(&report)?,
            output.display()
        ),
        Err(error) => {
            write_json(
                &output.join("run-error.json"),
                &json!({"error":error.to_string()}),
            )?;
            return Err(error);
        }
    }
    Ok(())
}
