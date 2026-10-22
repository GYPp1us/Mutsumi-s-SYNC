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
    let prompts = config.prompts()?;
    let model = Arc::new(OpenAiCompatible::new(config.provider.clone())?);
    let ledger = Ledger::open(Path::new(&config.database)).await?;
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
            model: model.clone(),
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
