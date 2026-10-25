//! Replay adapters around the normal Runtime, context builder, reviewer, and ledger.
mod evaluation;
pub mod report;
use crate::{
    benchmark::write_json,
    config::Config,
    executor::Executor,
    ledger::Ledger,
    media::MediaService,
    provider::OpenAiCompatible,
    rendering::RendererWorker,
    review::ModelReviewer,
    routing::{MainModelRouter, QuickReplies},
    runtime::Runtime,
    scheduler::{ExternalEvent, Ingress, Scheduler, Source},
    simulator::{Simulator, SimulatorSender, bounded_bytes},
    tools::ToolRegistry,
    vision::VisionCache,
};
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scene {
    pub version: u32,
    pub name: String,
    pub description: String,
    pub sources: BTreeMap<String, Source>,
    pub asset_targets: BTreeMap<String, Vec<String>>,
    pub seed: Vec<SeedEvent>,
    pub events: Vec<TimedEvent>,
    pub triggers: Vec<TriggeredEvent>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeedEvent {
    pub source: String,
    pub event: ExternalEvent,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TimedEvent {
    pub source: String,
    pub at_ms: u64,
    pub event: ExternalEvent,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TriggeredEvent {
    pub on_kind: String,
    pub target_ref: String,
    pub source: String,
    pub delay_ms: u64,
    pub timeout_ms: u64,
    pub event: ExternalEvent,
}

pub struct Options {
    pub config: PathBuf,
    pub scenario: PathBuf,
    pub assets: PathBuf,
    pub output: PathBuf,
    pub repeats: usize,
    pub judge: bool,
    pub jitter_seed: u64,
}

pub fn inspect(path: &Path) -> Result<Value> {
    let scene: Scene = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
    if scene.version != 2 || scene.sources.is_empty() || scene.events.is_empty() {
        bail!("invalid replay scenario")
    }
    for source in scene.sources.values() {
        if source.actor.is_empty()
            || source.conversation.is_empty()
            || !matches!(
                source.kind.as_str(),
                "human" | "group_member" | "service" | "email" | "child_agent"
            )
        {
            bail!("invalid source binding")
        }
    }
    Ok(
        json!({"name":scene.name,"description":scene.description,"sources":scene.sources.len(),"seed_events":scene.seed.len(),"scheduled_events":scene.events.len(),"inflight_triggers":scene.triggers.len(),"gold_in_model_context":false,"context_builder":"src/context.rs","runtime":"src/runtime.rs"}),
    )
}

pub async fn run(options: Options) -> Result<Value> {
    let inspected = inspect(&options.scenario)?;
    if options.output.join("manifest.json").exists() {
        bail!("output exists; use a new replay directory")
    }
    tokio::fs::create_dir_all(&options.output).await?;
    let mut config = Config::load(&options.config)?;
    if config.provider.model != "deepseek/deepseek-v4.1-flash" {
        bail!("this replay is configured for the user-selected Command Code DeepSeek V4.1 Flash")
    }
    config.database = options
        .output
        .join("runtime.sqlite3")
        .to_string_lossy()
        .into();
    let media_config = config
        .media
        .as_mut()
        .context("playground needs media configuration")?;
    media_config.root = options.output.join("media").to_string_lossy().into();
    let worker = Arc::new(
        RendererWorker::new(
            Path::new(&media_config.worker_executable),
            &options.output.join("render-jobs"),
            Duration::from_secs(media_config.worker_timeout_seconds),
        )
        .await?,
    );
    let simulator = Simulator::start(options.output.join("simulator")).await?;
    let result = run_with_transport(&options, &config, &simulator, worker.clone(), inspected).await;
    let worker_shutdown = worker.shutdown().await;
    let simulator_shutdown = simulator.shutdown().await;
    let report = result?;
    worker_shutdown?;
    simulator_shutdown?;
    Ok(report)
}

async fn run_with_transport(
    options: &Options,
    config: &Config,
    simulator: &Simulator,
    worker: Arc<RendererWorker>,
    inspected: Value,
) -> Result<Value> {
    let scene: Scene = serde_yaml::from_str(&std::fs::read_to_string(&options.scenario)?)?;
    let ledger = Ledger::open(Path::new(&config.database)).await?;
    if let Some(profile) = &config.profile {
        ledger
            .install_profile(serde_yaml::from_str(&std::fs::read_to_string(profile)?)?)
            .await?;
    }
    let media = Arc::new(
        MediaService::new(
            ledger.clone(),
            &options.output.join("media/objects"),
            worker,
            &config.directory,
        )
        .await?,
    );
    let prompts = config.prompts()?;
    let main = Arc::new(OpenAiCompatible::for_purpose(
        config.provider.clone(),
        "main",
    )?);
    let reviewer = Arc::new(OpenAiCompatible::for_purpose(
        config.provider.clone(),
        "review",
    )?);
    let vision_provider = Arc::new(OpenAiCompatible::for_purpose(
        config.provider.clone(),
        "vision",
    )?);
    let vision = Arc::new(VisionCache::new(
        media.clone(),
        vision_provider.clone(),
        config.provider.model.clone(),
        prompts.vision.clone(),
    )?);
    *simulator.state.vision.lock().unwrap() = Some(vision);
    let assets: Vec<Value> = serde_json::from_slice(
        &std::fs::read(options.assets.join("resolved.json"))
            .context("run scripts/fetch_playground_assets.py first")?,
    )?;
    let mut ids = BTreeMap::new();
    let mut originals = BTreeMap::new();
    let mut input_media = Vec::new();
    tokio::fs::create_dir_all(options.output.join("inputs")).await?;
    for asset in &assets {
        let alias = asset["id"].as_str().context("missing asset id")?;
        let filename = asset["cache_file"]
            .as_str()
            .context("missing asset filename")?;
        if Path::new(filename).components().count() != 1 {
            bail!("asset filename must be a basename")
        }
        let bytes = std::fs::read(options.assets.join(filename))?;
        let sha = format!("{:x}", Sha256::digest(&bytes));
        if asset["sha256"] != sha {
            bail!("downloaded asset checksum changed")
        }
        let targets = scene
            .asset_targets
            .get(alias)
            .context("missing media audience declaration")?
            .clone();
        if targets
            .iter()
            .any(|t| !config.directory.targets.contains_key(t))
        {
            bail!("unknown asset audience")
        }
        ids.insert(
            alias.to_owned(),
            simulator.add_asset(bytes.clone(), targets),
        );
        let extension = image::guess_format(&bytes)?.extensions_str()[0];
        let file = format!("inputs/{sha}.{extension}");
        tokio::fs::write(options.output.join(&file), &bytes).await?;
        input_media.push(json!({"asset_id":alias,"file":file,"sha256":sha,"bytes":bytes.len(),"source":asset["source"],"rights":asset["rights"]}));
        originals.insert(sha, bytes);
    }
    write_json(
        &options.output.join("manifest.json"),
        &json!({"scene":inspected,"requested_model":config.provider.model,"base_url":config.provider.base_url,"scenario_sha256":format!("{:x}",Sha256::digest(std::fs::read(&options.scenario)?)),"prompts_sha256":format!("{:x}",Sha256::digest(std::fs::read(&config.prompts)?)),"runner_sha256":format!("{:x}",Sha256::digest(std::fs::read(std::env::current_exe()?)?)),"jitter_seed":options.jitter_seed,"assets":assets,"started_at":chrono::Utc::now(),"transport":"onebot_simulator","not_real_qq":true}),
    )?;
    let mut runs = Vec::new();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(180))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    for repeat in 0..options.repeats {
        let session = format!(
            "{}-{}",
            scene.name,
            if repeat == 0 {
                "cold".into()
            } else {
                format!("warm-{repeat}")
            }
        );
        let inlet = Arc::new(Ingress {
            ledger: ledger.clone(),
            session: session.clone(),
            sources: scene.sources.clone(),
            changed: Arc::new(Notify::new()),
        });
        simulator
            .state
            .ingress
            .lock()
            .unwrap()
            .insert(session.clone(), inlet.clone());
        let vision_before = vision_provider.metrics().len();
        let downloads_before = simulator.state.download_requests.load(Ordering::Relaxed);
        let uploads_before = simulator.state.upload_requests.load(Ordering::Relaxed);
        println!(
            "{session}: seeding {} historical events through HTTP ingress",
            scene.seed.len()
        );
        for seed in &scene.seed {
            post_event(
                &client,
                &simulator.state.base_url,
                &session,
                &seed.source,
                &resolve(&seed.event, &ids)?,
            )
            .await?;
        }
        simulator.wait_media().await?;
        let seeded = ledger.stream_events(&session, 0).await?;
        let through = seeded
            .iter()
            .filter_map(|e| e["seq"].as_i64())
            .max()
            .unwrap_or(0);
        ledger.checkpoint(&session, through).await?;
        let runtime = Arc::new(Runtime {
            config: config.clone(),
            prompts: prompts.clone(),
            model: main.clone(),
            executor: Executor {
                ledger: ledger.clone(),
                reviewer: Arc::new(ModelReviewer {
                    model: reviewer.clone(),
                    system: prompts.reviewer.clone(),
                }),
                sender: Arc::new(SimulatorSender {
                    media: media.clone(),
                    base_url: simulator.state.base_url.clone(),
                    namespace: simulator.state.namespace.clone(),
                    run_id: session.clone(),
                    client: client.clone(),
                }),
                directory: config.directory.clone(),
                tools: ToolRegistry {
                    media: Some(media.clone()),
                    workspace: None,
                },
                cancel: CancellationToken::new(),
            },
            router: Arc::new(MainModelRouter),
            quick_replies: QuickReplies::load(Path::new(&config.quick_replies))?,
        });
        let done = Arc::new(AtomicBool::new(false));
        let scheduler = Scheduler {
            runtime: runtime.clone(),
            ingress: inlet.clone(),
        };
        let started = Instant::now();
        println!("{session}: concurrent replay started");
        let run_future = async {
            let feeder = async {
                let result = feed(
                    &scene,
                    &ids,
                    &client,
                    simulator,
                    &session,
                    options.jitter_seed,
                )
                .await;
                done.store(true, Ordering::Release);
                inlet.changed.notify_one();
                result
            };
            let (feed, outcomes) = tokio::join!(feeder, scheduler.drain(done.clone()));
            Ok::<_, anyhow::Error>((feed?, outcomes?))
        };
        let (triggers, outcomes) = match tokio::time::timeout(Duration::from_secs(900), run_future)
            .await
        {
            Ok(result) => result?,
            Err(_) => {
                runtime.executor.cancel.cancel();
                bail!("replay exceeded 15-minute budget; simulator retained no external QQ effects")
            }
        };
        let events = ledger.stream_events(&session, 0).await?;
        let receiver = client
            .get(format!("{}/messages", simulator.state.base_url))
            .send()
            .await?
            .json::<Vec<Value>>()
            .await?
            .into_iter()
            .filter(|m| m["run_id"] == session)
            .collect::<Vec<_>>();
        let received_media =
            verify_receiver(&client, simulator, &receiver, &originals, &options.output).await?;
        let checks = report::deterministic(&events, &receiver, &triggers, &received_media, &assets);
        let run = json!({"session":session,"elapsed_ms":started.elapsed().as_millis(),"events":events,"receiver":receiver,"received_media":received_media,"triggers":triggers,"outcomes":outcomes,"checks":checks,"vision_calls_including_seed":vision_provider.metrics().len()-vision_before,"http_downloads":simulator.state.download_requests.load(Ordering::Relaxed)-downloads_before,"http_uploads":simulator.state.upload_requests.load(Ordering::Relaxed)-uploads_before,"remaining_pending":ledger.pending(&session).await?.len()});
        println!(
            "{session}: {} simulator messages, {} vision calls",
            run["receiver"].as_array().unwrap().len(),
            run["vision_calls_including_seed"]
        );
        write_json(&options.output.join(format!("{session}.json")), &run)?;
        runs.push(run);
    }
    let (evaluation, evaluator_metrics) = if options.judge {
        evaluation::evaluate(config, &prompts.evaluator, &runs, &assets, &options.assets).await?
    } else {
        (Value::Null, vec![])
    };
    let metrics = json!({"main":main.metrics(),"review":reviewer.metrics(),"vision":vision_provider.metrics(),"evaluation":evaluator_metrics});
    let report = json!({"runs":runs,"evaluation":evaluation,"provider_metrics":metrics,"input_media":input_media,"limits":["constructed multi-source replay, not captured production traffic","real public image bytes; assets remain in private ignored cache","simulator upload/send/receive verified, no real QQ","qualitative evaluator uses the same model family, not independent human ground truth","context over budget is rejected; no production compaction claim"]});
    write_json(&options.output.join("report.json"), &report)?;
    report::html(&options.output, &report)?;
    Ok(report::summary(&report))
}

fn resolve(event: &ExternalEvent, ids: &BTreeMap<String, String>) -> Result<ExternalEvent> {
    let mut event = event.clone();
    event.media = event
        .media
        .iter()
        .map(|alias| {
            if alias == "unavailable" {
                Ok("missing_resource".into())
            } else {
                ids.get(alias)
                    .cloned()
                    .context("unknown scenario media alias")
            }
        })
        .collect::<Result<_>>()?;
    Ok(event)
}
async fn post_event(
    client: &reqwest::Client,
    base: &str,
    session: &str,
    source: &str,
    event: &ExternalEvent,
) -> Result<Value> {
    let response = client
        .post(format!("{base}/events/{session}/{source}"))
        .json(event)
        .send()
        .await?;
    if !response.status().is_success() {
        bail!("simulated ingress rejected event {}", event.id)
    }
    Ok(response.json().await?)
}
async fn feed(
    scene: &Scene,
    ids: &BTreeMap<String, String>,
    client: &reqwest::Client,
    simulator: &Simulator,
    session: &str,
    jitter_seed: u64,
) -> Result<Vec<Value>> {
    let started = tokio::time::Instant::now();
    let mut streams = BTreeMap::<String, Vec<TimedEvent>>::new();
    for event in &scene.events {
        streams
            .entry(event.source.clone())
            .or_default()
            .push(event.clone());
    }
    let mut tasks = tokio::task::JoinSet::new();
    for (source, mut events) in streams {
        events.sort_by_key(|e| e.at_ms);
        let client = client.clone();
        let base = simulator.state.base_url.clone();
        let session = session.to_owned();
        let ids = ids.clone();
        tasks.spawn(async move {
            for scheduled in events {
                let hash =
                    Sha256::digest(format!("{jitter_seed}:{}:{}", source, scheduled.event.id));
                let jitter = u64::from(hash[0]) % 90;
                tokio::time::sleep_until(started + Duration::from_millis(scheduled.at_ms + jitter))
                    .await;
                post_event(
                    &client,
                    &base,
                    &session,
                    &source,
                    &resolve(&scheduled.event, &ids)?,
                )
                .await?;
            }
            Ok::<_, anyhow::Error>(())
        });
    }
    let mut trigger_tasks = tokio::task::JoinSet::new();
    for trigger in &scene.triggers {
        let trigger = trigger.clone();
        let ledger = simulator.state.ingress.lock().unwrap()[session]
            .ledger
            .clone();
        let client = client.clone();
        let base = simulator.state.base_url.clone();
        let session = session.to_owned();
        let ids = ids.clone();
        trigger_tasks.spawn(async move{
            let start=Instant::now();
            loop {
                let events=ledger.stream_events(&session,0).await?;
                if let Some(cause)=events.iter().find(|e|e["kind"]==trigger.on_kind && e["data"]["action"]["message"]["target_ref"]==trigger.target_ref && (trigger.on_kind!="action_result" || e["data"]["status"]=="succeeded")) {
                    tokio::time::sleep(Duration::from_millis(trigger.delay_ms)).await;
                    let result=post_event(&client,&base,&session,&trigger.source,&resolve(&trigger.event,&ids)?).await?;
                    return Ok::<_,anyhow::Error>(json!({"event_id":trigger.event.id,"status":"fired","cause_seq":cause["seq"],"injected_seq":result["seq"]}))
                }
                if start.elapsed()>Duration::from_millis(trigger.timeout_ms){return Ok(json!({"event_id":trigger.event.id,"status":"not_reached"}))}
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
        });
    }
    while let Some(result) = tasks.join_next().await {
        result??;
    }
    let mut triggers = Vec::new();
    while let Some(result) = trigger_tasks.join_next().await {
        triggers.push(result??);
    }
    simulator.wait_media().await?;
    Ok(triggers)
}

async fn verify_receiver(
    client: &reqwest::Client,
    simulator: &Simulator,
    messages: &[Value],
    originals: &BTreeMap<String, Vec<u8>>,
    root: &Path,
) -> Result<Vec<Value>> {
    tokio::fs::create_dir_all(root.join("receiver")).await?;
    let mut verified = BTreeMap::new();
    for message in messages {
        for segment in message["message"]
            .as_array()
            .context("invalid receiver array")?
        {
            if segment["type"] != "image" {
                continue;
            }
            let reference = segment["data"]["file"]
                .as_str()
                .context("missing received reference")?;
            let sha = reference
                .strip_prefix(&format!("sim://{}/", simulator.state.namespace))
                .context("unknown receiver namespace")?;
            let bytes = bounded_bytes(
                client
                    .get(format!("{}/uploaded/{sha}", simulator.state.base_url))
                    .send()
                    .await?,
            )
            .await?;
            let matches_hash = format!("{:x}", Sha256::digest(&bytes)) == sha;
            let original_equal = originals.get(sha).map(|original| original == &bytes);
            let format = image::guess_format(&bytes)?;
            let extension = format.extensions_str()[0];
            let file = format!("receiver/{sha}.{extension}");
            tokio::fs::write(root.join(&file), &bytes).await?;
            verified.insert(sha.to_owned(),json!({"sha256":sha,"bytes":bytes.len(),"matches_hash":matches_hash,"matches_original":original_equal,"format":format!("{format:?}"),"file":file}));
        }
    }
    Ok(verified.into_values().collect())
}
