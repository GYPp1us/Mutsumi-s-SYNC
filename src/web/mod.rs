//! A small authenticated trial server around the shared orchestration runtime.
pub mod roles;
mod routes;
use crate::{
    config::Config,
    executor::Executor,
    ledger::Ledger,
    media::MediaService,
    napcat::{LiveSender, NapcatClient, NapcatConfig},
    provider::{Model, OpenAiCompatible},
    rendering::RendererWorker,
    review::ModelReviewer,
    routing::{MainModelRouter, QuickReplies},
    runtime::Runtime,
    scheduler::{ExternalEvent, Ingress, Scheduler, Source},
    tools::ToolRegistry,
    vision::VisionCache,
    workspace::WorkspaceTools,
};
use anyhow::{Context, Result, bail};
pub use routes::router;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::Path,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::{Mutex as AsyncMutex, Notify};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    pub runtime_config: String,
    pub listen: String,
    pub public_origin: String,
    pub base_path: String,
    pub access_key_env: String,
    pub onebot_hook_key_env: String,
    pub owner_source: Source,
    pub hooks: BTreeMap<String, Hook>,
    pub napcat: Option<NapcatConfig>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Hook {
    pub source: Source,
    pub token_env: String,
}

pub struct WebState {
    pub provider_configured: bool,
    pub settings: ServerConfig,
    pub runtime: Arc<Runtime>,
    pub ingress: Arc<Ingress>,
    pub media: Arc<MediaService>,
    pub vision: Arc<VisionCache>,
    pub napcat: Option<Arc<NapcatClient>>,
    pub qq_enabled: Arc<AtomicBool>,
    pub busy: AtomicBool,
    pub needs_attention: AtomicBool,
    pub last_outcome: Mutex<Value>,
    pub cancel: CancellationToken,
    pub jobs: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    pub(crate) access_key: String,
    pub(crate) onebot_hook_key: String,
    pub(crate) hook_keys: BTreeMap<String, String>,
    pub(crate) sessions: Mutex<BTreeMap<String, Instant>>,
    pub(crate) login_attempts: Mutex<Vec<Instant>>,
    pub(crate) roles: RwLock<roles::RoleBook>,
    pub(crate) control: AsyncMutex<()>,
    pub(crate) profile_control: AsyncMutex<()>,
    profile: Value,
}

impl WebState {
    pub async fn build(settings: ServerConfig) -> Result<Arc<Self>> {
        let mut config = Config::load(Path::new(&settings.runtime_config))?;
        if !settings
            .listen
            .parse::<std::net::SocketAddr>()?
            .ip()
            .is_loopback()
        {
            bail!("trial server must listen on loopback behind HTTPS")
        }
        if !settings.base_path.starts_with('/')
            || settings.base_path.ends_with('/')
            || settings.base_path.contains("..")
        {
            bail!("base_path must be /name without trailing slash")
        }
        let origin = reqwest::Url::parse(&settings.public_origin)?;
        if origin.scheme() != "https"
            && !matches!(origin.host_str(), Some("127.0.0.1" | "localhost"))
        {
            bail!("public origin requires HTTPS")
        }
        if config
            .directory
            .targets
            .values()
            .any(|t| !matches!(t.transport.as_str(), "webui" | "napcat"))
        {
            bail!("web server only supports explicit webui and napcat targets")
        }
        let access_key = secret(&settings.access_key_env)?;
        let onebot_hook_key = secret(&settings.onebot_hook_key_env)?;
        let mut sources = BTreeMap::from([("owner_web".into(), settings.owner_source.clone())]);
        let mut hook_keys = BTreeMap::new();
        if settings.owner_source.relationship != "owner"
            || settings.owner_source.reply_target.as_deref() != Some("web_owner")
        {
            bail!("invalid web owner binding")
        }
        for (name, hook) in &settings.hooks {
            if name == "owner_web"
                || name == "qq_owner"
                || name.is_empty()
                || !name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
                || !matches!(
                    hook.source.kind.as_str(),
                    "service" | "email" | "child_agent"
                )
                || hook.source.relationship == "owner"
            {
                bail!("external hooks cannot impersonate a human owner")
            }
            sources.insert(name.clone(), hook.source.clone());
            hook_keys.insert(name.clone(), secret(&hook.token_env)?);
        }
        let napcat = settings
            .napcat
            .clone()
            .map(NapcatClient::new)
            .transpose()?
            .map(Arc::new);
        if let Some(client) = &napcat {
            if client
                .config
                .targets
                .iter()
                .any(|(target, _)| !config.directory.targets.contains_key(target))
            {
                bail!("QQ binding has no directory target")
            }
            sources.insert(
                "qq_owner".into(),
                Source {
                    actor: format!("qq:{}", client.config.owner_id),
                    kind: "human".into(),
                    relationship: "owner".into(),
                    conversation: "qq_owner".into(),
                    reply_target: Some("qq_owner".into()),
                },
            );
        }
        let prompts = config.prompts()?;
        let ledger = Ledger::open(Path::new(&config.database)).await?;
        let role_book = match ledger
            .cache_get("runtime", "profile")
            .await?
            .and_then(|p| p.get("qq_roles").cloned())
        {
            Some(book) => serde_json::from_value(book)?,
            None => roles::RoleBook::seed(settings.napcat.as_ref()),
        };
        if let Some(client) = &napcat {
            role_book.validate(&client.config.bot_id)?;
            config.directory = role_book.directory(&config.directory, &client.config.bot_id, true);
        }
        let profile = if let Some(path) = &config.profile {
            serde_yaml::from_str(&std::fs::read_to_string(path)?)?
        } else {
            json!({"owner":settings.owner_source.actor})
        };
        let media_config = config
            .media
            .as_ref()
            .context("web server needs media configuration")?;
        let worker = Arc::new(
            RendererWorker::new(
                Path::new(&media_config.worker_executable),
                &Path::new(&media_config.root).join("render-jobs"),
                Duration::from_secs(media_config.worker_timeout_seconds),
            )
            .await?,
        );
        let media = Arc::new(
            MediaService::new(
                ledger.clone(),
                &Path::new(&media_config.root).join("objects"),
                worker,
                &config.directory,
            )
            .await?,
        );
        let provider_configured =
            std::env::var(&config.provider.api_key_env).is_ok_and(|v| !v.trim().is_empty());
        let vision = Arc::new(VisionCache::new(
            media.clone(),
            provider(&config.provider, "vision")?,
            config.provider.model.clone(),
            prompts.vision.clone(),
        )?);
        let qq_enabled = Arc::new(AtomicBool::new(false)); // A restart always starts with live QQ disabled.
        let cancel = CancellationToken::new();
        let runtime = Arc::new(Runtime {
            config: config.clone(),
            prompts: prompts.clone(),
            model: provider(&config.provider, "main")?,
            executor: Executor {
                ledger: ledger.clone(),
                reviewer: Arc::new(ModelReviewer {
                    model: provider(&config.provider, "review")?,
                    system: prompts.reviewer,
                }),
                sender: Arc::new(LiveSender {
                    ledger: ledger.clone(),
                    media: media.clone(),
                    napcat: napcat.clone(),
                    qq_enabled: qq_enabled.clone(),
                    directory: config.directory.clone(),
                    bindings: role_book.bindings(),
                    session: "live".into(),
                }),
                directory: config.directory.clone(),
                tools: ToolRegistry {
                    media: Some(media.clone()),
                    workspace: match &config.workspace {
                        Some(workspace) => Some(Arc::new(WorkspaceTools::new(workspace).await?)),
                        None => None,
                    },
                },
                cancel: cancel.clone(),
            },
            router: Arc::new(MainModelRouter),
            quick_replies: QuickReplies::load(Path::new(&config.quick_replies))?,
        });
        let unknown = ledger
            .db(|db| {
                Ok(db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM actions WHERE status='unknown')",
                    [],
                    |r| r.get::<_, bool>(0),
                )?)
            })
            .await?;
        let needs_attention = unknown
            || ledger
                .cache_get("web", "needs_attention")
                .await?
                .is_some_and(|v| v == true);
        let state = Arc::new(Self {
            provider_configured,
            settings,
            runtime,
            ingress: Arc::new(Ingress {
                ledger,
                session: "live".into(),
                sources,
                changed: Arc::new(Notify::new()),
            }),
            media,
            vision,
            napcat,
            qq_enabled,
            busy: AtomicBool::new(false),
            needs_attention: AtomicBool::new(needs_attention),
            last_outcome: Mutex::new(Value::Null),
            cancel,
            jobs: Mutex::new(vec![]),
            access_key,
            onebot_hook_key,
            hook_keys,
            sessions: Mutex::new(BTreeMap::new()),
            login_attempts: Mutex::new(vec![]),
            roles: RwLock::new(role_book),
            control: AsyncMutex::new(()),
            profile_control: AsyncMutex::new(()),
            profile,
        });
        state.update_profile().await?;
        Ok(state)
    }
    pub async fn update_profile(&self) -> Result<()> {
        let _profile_control = self.profile_control.lock().await;
        let book = self.roles.read().unwrap().clone();
        self.install_roles_profile(&book).await
    }
    pub(crate) async fn install_roles_profile(&self, book: &roles::RoleBook) -> Result<()> {
        let mut profile = self.profile.clone();
        profile["qq_roles"] = serde_json::to_value(book)?;
        if let Some(owner) = book
            .contacts
            .values()
            .find(|c| c.role == roles::Role::Owner)
        {
            profile["owner"]["qq_id"] = json!(owner.qq_id);
            profile["owner"]["name"] = json!(owner.name);
        }
        profile["host_transport_state"] = json!({"qq_enabled":self.qq_enabled.load(Ordering::Acquire),"web_target":"web_owner","qq_scope":book.scope,"web_receipt":"stored in authenticated web UI; not proof the browser has read it","napcat_receipt":"API accepted with a message id; not proof a human has read it"});
        self.ingress.ledger.install_profile(profile).await
    }
    pub(crate) fn scoped_runtime(&self) -> Result<Arc<Runtime>> {
        let Some(client) = &self.napcat else {
            return Ok(self.runtime.clone());
        };
        let book = self.roles.read().unwrap().clone();
        let mut config = self.runtime.config.clone();
        config.directory = book.directory(&config.directory, &client.config.bot_id, false);
        let mut executor = self.runtime.executor.clone();
        executor.directory = config.directory.clone();
        executor.sender = Arc::new(LiveSender {
            ledger: self.ingress.ledger.clone(),
            media: self.media.clone(),
            napcat: self.napcat.clone(),
            qq_enabled: self.qq_enabled.clone(),
            directory: config.directory.clone(),
            bindings: book.bindings(),
            session: "live".into(),
        });
        Ok(Arc::new(Runtime {
            config,
            prompts: self.runtime.prompts.clone(),
            model: self.runtime.model.clone(),
            executor,
            router: self.runtime.router.clone(),
            quick_replies: self.runtime.quick_replies.clone(),
        }))
    }
    pub async fn set_attention(&self, value: bool) -> Result<()> {
        self.needs_attention.store(value, Ordering::Release);
        self.ingress
            .ledger
            .cache_put("web", "needs_attention", json!(value))
            .await
    }
    pub async fn submit(
        self: &Arc<Self>,
        source: &str,
        event: &ExternalEvent,
        owner: bool,
    ) -> Result<(i64, bool)> {
        for id in &event.media {
            self.media.bytes_for_target(id, "web_owner").await?;
        }
        let result = self.ingress.receive(source, event).await?;
        if owner && result.1 {
            self.set_attention(false).await?;
            self.ingress.changed.notify_one();
        }
        if result.1 && !event.media.is_empty() {
            let state = self.clone();
            let event = event.clone();
            self.spawn(async move {
                let mut observations=Vec::new();
                for (position,id) in event.media.iter().enumerate() {
                    let result=async {let (record,bytes)=state.media.bytes_for_target(id,"web_owner").await?;let (record,description,hit)=state.vision.ingest(bytes,record.allowed_targets).await?;Ok::<_,anyhow::Error>(json!({"media":record.reference(),"visual_observation":description,"vision_cache_hit":hit}))}.await;
                    observations.push(match result{Ok(data)=>json!({"position":position,"status":"available","data":data}),Err(_)=>json!({"position":position,"status":"unavailable","reason":"visual observation unavailable; original media is retained"})});
                }
                if let Err(error)=state.ingress.observed_media(&event.id,json!(observations)).await {eprintln!("media observation persistence failed: {error}");}
            });
        }
        Ok(result)
    }
    pub fn spawn(&self, task: impl std::future::Future<Output = ()> + Send + 'static) {
        let mut jobs = self.jobs.lock().unwrap();
        jobs.retain(|j| !j.is_finished());
        jobs.push(tokio::spawn(task));
    }
    pub async fn work(self: Arc<Self>) -> Result<()> {
        loop {
            if self.cancel.is_cancelled() {
                break;
            }
            if self.needs_attention.load(Ordering::Acquire)
                || self.ingress.ledger.pending("live").await?.is_empty()
            {
                tokio::select! {_=self.cancel.cancelled()=>break,_=self.ingress.changed.notified()=>()};
                continue;
            }
            tokio::time::sleep(Duration::from_millis(180)).await;
            // Role edits are atomic between attention turns; ingress stays concurrent.
            let _control = self.control.lock().await;
            let scheduler = Scheduler {
                runtime: self.scoped_runtime()?,
                ingress: self.ingress.clone(),
            };
            self.busy.store(true, Ordering::Release);
            self.ingress
                .ledger
                .event("live", "attention", "attention_started", json!({}))
                .await?;
            let outcome = scheduler.step().await;
            self.busy.store(false, Ordering::Release);
            match outcome {
                Ok(outcome) => {
                    if !matches!(outcome.status.as_str(), "completed" | "silent") {
                        self.set_attention(true).await?;
                    }
                    *self.last_outcome.lock().unwrap() = serde_json::to_value(outcome)?;
                }
                Err(_) => {
                    self.set_attention(true).await?;
                    self.ingress
                        .ledger
                        .event(
                            "live",
                            "attention",
                            "runtime_error",
                            json!({"reason":"runtime stopped; inspect local server diagnostics"}),
                        )
                        .await?;
                }
            }
        }
        Ok(())
    }
    pub async fn shutdown(&self) -> Result<()> {
        self.cancel.cancel();
        let jobs = std::mem::take(&mut *self.jobs.lock().unwrap());
        for job in jobs {
            let _ = job.await;
        }
        self.media.worker.shutdown().await
    }
}

fn secret(name: &str) -> Result<String> {
    let value = std::env::var(name).context("missing server credential")?;
    if value.len() < 24 {
        bail!("server credentials must contain at least 24 characters")
    };
    Ok(value)
}

struct UnconfiguredModel;
#[async_trait::async_trait]
impl Model for UnconfiguredModel {
    async fn complete(
        &self,
        _messages: &[Value],
        _tools: &[Value],
    ) -> Result<crate::protocol::AssistantRound> {
        bail!("model provider credentials are not configured")
    }
}
fn provider(config: &crate::config::ProviderConfig, purpose: &str) -> Result<Arc<dyn Model>> {
    if std::env::var(&config.api_key_env).is_ok_and(|v| !v.trim().is_empty()) {
        Ok(Arc::new(OpenAiCompatible::for_purpose(
            config.clone(),
            purpose,
        )?))
    } else {
        Ok(Arc::new(UnconfiguredModel))
    }
}

pub async fn serve(path: &Path) -> Result<()> {
    let settings: ServerConfig = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
    let address = settings.listen.clone();
    let state = WebState::build(settings).await?;
    let work_state = state.clone();
    let worker = tokio::spawn(async move {
        // A failed or panicked attention loop must not leave a healthy-looking HTTP service.
        let _stop_on_exit = work_state.cancel.clone().drop_guard();
        work_state.work().await
    });
    let listener = tokio::net::TcpListener::bind(&address).await?;
    let shutdown = state.cancel.clone();
    let signal = tokio::spawn(async move {
        #[cfg(unix)]
        {
            let mut term =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("SIGTERM handler");
            tokio::select! {_=tokio::signal::ctrl_c()=>(),_=term.recv()=>()};
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
        shutdown.cancel();
    });
    eprintln!("trial WebUI listening on {address}");
    let result = axum::serve(listener, router(state.clone()))
        .with_graceful_shutdown(state.cancel.clone().cancelled_owned())
        .await;
    state.cancel.cancel();
    let work = worker.await;
    let close = state.shutdown().await;
    signal.abort();
    let _ = signal.await;
    result?;
    work??;
    close?;
    Ok(())
}
