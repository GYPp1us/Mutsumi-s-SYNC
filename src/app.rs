use crate::{
    config::Config,
    executor::Executor,
    jev::{JevClient, JevPrompts, JevRouter},
    ledger::Ledger,
    media::MediaService,
    onebot::OneBotOutbox,
    provider::OpenAiCompatible,
    rendering::RendererWorker,
    review::ModelReviewer,
    routing::{MainModelRouter, QuickReplies, Router},
    runtime::Runtime,
    sender::{Sender, TerminalSender},
    tools::ToolRegistry,
};
use anyhow::Result;
use std::{path::Path, sync::Arc, time::Duration};
use tokio_util::sync::CancellationToken;

pub async fn build(config: Config) -> Result<Runtime> {
    if config
        .directory
        .targets
        .values()
        .any(|t| t.transport == "onebot_simulator")
    {
        anyhow::bail!("simulator targets require the playground transport driver")
    }
    let model = Arc::new(OpenAiCompatible::for_purpose(
        config.provider.clone(),
        "main",
    )?);
    let reviewer = Arc::new(OpenAiCompatible::for_purpose(
        config.provider.clone(),
        "review",
    )?);
    build_with_providers(config, model, reviewer).await
}

pub async fn build_with_providers(
    config: Config,
    model: Arc<dyn crate::provider::Model>,
    reviewer: Arc<dyn crate::provider::Model>,
) -> Result<Runtime> {
    let prompts = config.prompts()?;
    let ledger = Ledger::open(Path::new(&config.database)).await?;
    if let Some(path) = &config.profile {
        let profile: serde_json::Value = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
        ledger.install_profile(profile).await?;
    }
    let quick_replies = QuickReplies::load(Path::new(&config.quick_replies))?;
    let router: Arc<dyn Router> = if let Some(jev) = &config.jev {
        Arc::new(JevRouter {
            client: JevClient::new(jev.clone())?,
            prompts: JevPrompts::load(Path::new(&jev.prompts))?,
            replies: QuickReplies::load(Path::new(&config.quick_replies))?,
        })
    } else {
        Arc::new(MainModelRouter)
    };
    let (sender, tools): (Arc<dyn Sender>, ToolRegistry) = if let Some(media) = &config.media {
        let worker = Arc::new(
            RendererWorker::new(
                Path::new(&media.worker_executable),
                &Path::new(&media.root).join("render-jobs"),
                Duration::from_secs(media.worker_timeout_seconds),
            )
            .await?,
        );
        let service = Arc::new(
            MediaService::new(
                ledger.clone(),
                &Path::new(&media.root).join("objects"),
                worker,
                &config.directory,
            )
            .await?,
        );
        (
            Arc::new(OneBotOutbox {
                media: service.clone(),
                root: Path::new(&media.outbox).to_owned(),
            }),
            ToolRegistry {
                media: Some(service),
            },
        )
    } else {
        (
            Arc::new(TerminalSender(config.directory.clone())),
            ToolRegistry::default(),
        )
    };
    let executor = Executor {
        ledger,
        reviewer: Arc::new(ModelReviewer {
            model: reviewer,
            system: prompts.reviewer.clone(),
        }),
        sender,
        directory: config.directory.clone(),
        tools,
        cancel: CancellationToken::new(),
    };
    Ok(Runtime {
        config,
        prompts,
        model,
        executor,
        router,
        quick_replies,
    })
}
