use anyhow::Result;
use clap::Parser;
use std::path::PathBuf;
#[derive(Parser)]
#[command(about = "V4 trial WebUI, authenticated external events and NapCat")]
struct Args {
    #[arg(long, default_value = "config/server.example.yaml")]
    config: PathBuf,
}
#[tokio::main]
async fn main() -> Result<()> {
    mutsumi_runtime::web::serve(&Args::parse().config).await
}
