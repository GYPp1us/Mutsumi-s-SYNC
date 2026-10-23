//! A real loopback HTTP transport. Receipts prove simulator delivery only.
use crate::{
    media::MediaService,
    protocol::{OutgoingMessage, Segment},
    scheduler::{ExternalEvent, Ingress},
    sender::{Receipt, Sender},
    vision::VisionCache,
};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, State},
    http::StatusCode,
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Instant,
};
use tokio::{io::AsyncWriteExt, sync::Mutex as AsyncMutex, task::JoinHandle};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub struct IncomingAsset {
    pub bytes: Arc<Vec<u8>>,
    pub allowed_targets: Vec<String>,
}

pub struct SimulatorState {
    pub base_url: String,
    pub namespace: String,
    pub root: PathBuf,
    pub incoming: Mutex<BTreeMap<String, IncomingAsset>>,
    pub ingress: Mutex<BTreeMap<String, Arc<Ingress>>>,
    pub vision: Mutex<Option<Arc<VisionCache>>>,
    pub jobs: Mutex<Vec<JoinHandle<()>>>,
    pub fail_after_accept: AtomicBool,
    uploaded: AsyncMutex<BTreeMap<String, (Vec<u8>, String)>>,
    messages: AsyncMutex<Vec<Value>>,
    next: AtomicU64,
    pub upload_requests: AtomicU64,
    pub download_requests: AtomicU64,
}
pub struct Simulator {
    pub state: Arc<SimulatorState>,
    cancel: CancellationToken,
    server: JoinHandle<std::io::Result<()>>,
}

impl Simulator {
    pub async fn start(root: PathBuf) -> Result<Self> {
        tokio::fs::create_dir_all(&root).await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let base_url = format!("http://{}", listener.local_addr()?);
        let state = Arc::new(SimulatorState {
            base_url,
            namespace: uuid::Uuid::new_v4().simple().to_string(),
            root,
            incoming: Mutex::new(BTreeMap::new()),
            ingress: Mutex::new(BTreeMap::new()),
            vision: Mutex::new(None),
            jobs: Mutex::new(Vec::new()),
            fail_after_accept: AtomicBool::new(false),
            uploaded: AsyncMutex::new(BTreeMap::new()),
            messages: AsyncMutex::new(Vec::new()),
            next: AtomicU64::new(1),
            upload_requests: AtomicU64::new(0),
            download_requests: AtomicU64::new(0),
        });
        let app = Router::new()
            .route("/incoming/{id}", get(incoming))
            .route("/events/{session}/{source}", post(ingress))
            .route("/upload", post(upload))
            .route("/send", post(send))
            .route("/messages", get(messages))
            .route("/uploaded/{sha}", get(uploaded))
            .layer(DefaultBodyLimit::max(24 * 1024 * 1024))
            .with_state(state.clone());
        let cancel = CancellationToken::new();
        let shutdown = cancel.clone();
        let server = tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown.cancelled_owned())
                .await
        });
        Ok(Self {
            state,
            cancel,
            server,
        })
    }
    pub fn add_asset(&self, bytes: Vec<u8>, allowed_targets: Vec<String>) -> String {
        let id = format!("incoming_{:x}", Sha256::digest(&bytes));
        self.state.incoming.lock().unwrap().insert(
            id.clone(),
            IncomingAsset {
                bytes: Arc::new(bytes),
                allowed_targets,
            },
        );
        id
    }
    pub async fn wait_media(&self) -> Result<()> {
        loop {
            let jobs = std::mem::take(&mut *self.state.jobs.lock().unwrap());
            if jobs.is_empty() {
                break;
            }
            for job in jobs {
                job.await?;
            }
        }
        Ok(())
    }
    pub async fn shutdown(self) -> Result<()> {
        let jobs = self.wait_media().await;
        self.cancel.cancel();
        self.server.await??;
        jobs
    }
}

async fn incoming(
    State(state): State<Arc<SimulatorState>>,
    Path(id): Path<String>,
) -> Result<Bytes, StatusCode> {
    let asset = state
        .incoming
        .lock()
        .unwrap()
        .get(&id)
        .cloned()
        .ok_or(StatusCode::NOT_FOUND)?;
    state.download_requests.fetch_add(1, Ordering::Relaxed);
    Ok(Bytes::copy_from_slice(&asset.bytes))
}

async fn ingress(
    State(state): State<Arc<SimulatorState>>,
    Path((session, source)): Path<(String, String)>,
    Json(event): Json<ExternalEvent>,
) -> Result<Json<Value>, StatusCode> {
    let inlet = state
        .ingress
        .lock()
        .unwrap()
        .get(&session)
        .cloned()
        .ok_or(StatusCode::NOT_FOUND)?;
    let (seq, fresh) = inlet
        .receive(&source, &event)
        .await
        .map_err(|_| StatusCode::BAD_REQUEST)?;
    if fresh && !event.media.is_empty() {
        let state_copy = state.clone();
        let event_id = event.id.clone();
        let media_ids = event.media.clone();
        let job = tokio::spawn(async move {
            let vision = state_copy.vision.lock().unwrap().clone();
            let mut observations = Vec::new();
            for (position, id) in media_ids.iter().enumerate() {
                let result = ingest_asset(&state_copy, vision.clone(), id).await;
                observations.push(match result{Ok(value)=>json!({"position":position,"status":"available","data":value}),Err(error)=>json!({"position":position,"status":"unavailable","reason":error.to_string()})});
            }
            let _ = inlet.observed_media(&event_id, json!(observations)).await;
        });
        state.jobs.lock().unwrap().push(job);
    }
    Ok(Json(json!({"accepted":true,"duplicate":!fresh,"seq":seq})))
}

async fn ingest_asset(
    state: &SimulatorState,
    vision: Option<Arc<VisionCache>>,
    id: &str,
) -> Result<Value> {
    let started = Instant::now();
    let vision = vision.context("vision observer not configured")?;
    let asset = state
        .incoming
        .lock()
        .unwrap()
        .get(id)
        .cloned()
        .context("incoming resource unavailable")?;
    let uri = format!("{}/incoming/{id}", state.base_url);
    let cached = vision.media.ledger.cache_get("media-source", &uri).await?;
    let mut download_hit = false;
    let bytes = if let Some(value) = cached {
        if let Some(media_id) = value.as_str() {
            if let Some(target) = asset.allowed_targets.first() {
                match vision.media.bytes_for_target(media_id, target).await {
                    Ok((_, bytes)) => {
                        download_hit = true;
                        Some(bytes)
                    }
                    Err(_) => None,
                }
            } else {
                None
            }
        } else {
            None
        }
    } else {
        None
    };
    let bytes = match bytes {
        Some(bytes) => bytes,
        None => bounded_bytes(reqwest::Client::new().get(&uri).send().await?).await?,
    };
    let registered = vision
        .media
        .register(
            &bytes,
            "图像内容尚未识别",
            asset.allowed_targets.clone(),
            "raw-ingress",
        )
        .await?;
    vision
        .media
        .ledger
        .cache_put("media-source", &uri, json!(registered.id))
        .await?;
    match vision.ingest(bytes, asset.allowed_targets).await {
        Ok((record, description, vision_hit)) => Ok(
            json!({"media":record.reference(),"visual_observation":description,"observer_model":vision.model_id,"download_cache_hit":download_hit,"vision_cache_hit":vision_hit,"elapsed_ms":started.elapsed().as_millis()}),
        ),
        Err(error) => Ok(
            json!({"media":registered.reference(),"visual_status":"unavailable","reason":error.to_string(),"download_cache_hit":download_hit,"elapsed_ms":started.elapsed().as_millis()}),
        ),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Upload {
    sha256: String,
    mime_type: String,
    base64: String,
}
async fn upload(
    State(state): State<Arc<SimulatorState>>,
    Json(body): Json<Upload>,
) -> Result<Json<Value>, StatusCode> {
    state.upload_requests.fetch_add(1, Ordering::Relaxed);
    let bytes = STANDARD
        .decode(body.base64)
        .map_err(|_| StatusCode::BAD_REQUEST)?;
    if bytes.len() > 16 * 1024 * 1024 || format!("{:x}", Sha256::digest(&bytes)) != body.sha256 {
        return Err(StatusCode::BAD_REQUEST);
    }
    let mut uploaded = state.uploaded.lock().await;
    if !uploaded.contains_key(&body.sha256) {
        let path = state.root.join(format!("{}.bin", body.sha256));
        let mut file = tokio::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(path)
            .await
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        file.write_all(&bytes)
            .await
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        file.sync_all()
            .await
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        uploaded.insert(body.sha256.clone(), (bytes, body.mime_type));
    }
    Ok(Json(
        json!({"resource_ref":format!("sim://{}/{}",state.namespace,body.sha256)}),
    ))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SendRequest {
    action_id: String,
    run_id: String,
    target_ref: String,
    message: Vec<Value>,
}
async fn send(
    State(state): State<Arc<SimulatorState>>,
    Json(body): Json<SendRequest>,
) -> Result<Json<Value>, StatusCode> {
    if body.message.is_empty() || body.message.len() > 32 {
        return Err(StatusCode::BAD_REQUEST);
    }
    for segment in &body.message {
        match segment["type"].as_str() {
            Some("text")
                if segment["data"]["text"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty()) => {}
            Some("face") if segment["data"]["id"].as_str().is_some() => (),
            Some("image") => {
                let reference = segment["data"]["file"]
                    .as_str()
                    .ok_or(StatusCode::BAD_REQUEST)?;
                let sha = reference
                    .strip_prefix(&format!("sim://{}/", state.namespace))
                    .ok_or(StatusCode::BAD_REQUEST)?;
                if !state.uploaded.lock().await.contains_key(sha) {
                    return Err(StatusCode::BAD_REQUEST);
                }
            }
            _ => return Err(StatusCode::BAD_REQUEST),
        }
    }
    let mut messages = state.messages.lock().await;
    if let Some(existing) = messages.iter().find(|m| m["action_id"] == body.action_id) {
        if existing["target_ref"] != body.target_ref || existing["message"] != json!(body.message) {
            return Err(StatusCode::CONFLICT);
        }
        return Ok(Json(
            json!({"status":"ok","message_id":existing["message_id"]}),
        ));
    }
    let id = format!("sim_message_{}", state.next.fetch_add(1, Ordering::Relaxed));
    let mut record = serde_json::to_value(&body).map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    record["message_id"] = json!(id);
    record["received_at"] = json!(chrono::Utc::now().to_rfc3339());
    let mut file = tokio::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(state.root.join(format!(
            "receipt-{:x}.json",
            Sha256::digest(body.action_id.as_bytes())
        )))
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    file.write_all(record.to_string().as_bytes())
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    file.sync_all()
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    messages.push(record);
    if state.fail_after_accept.swap(false, Ordering::AcqRel) {
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }
    Ok(Json(json!({"status":"ok","message_id":id})))
}
async fn messages(State(state): State<Arc<SimulatorState>>) -> Json<Value> {
    Json(json!(state.messages.lock().await.clone()))
}
async fn uploaded(
    State(state): State<Arc<SimulatorState>>,
    Path(sha): Path<String>,
) -> Result<(axum::http::HeaderMap, Bytes), StatusCode> {
    let uploaded = state.uploaded.lock().await;
    let (bytes, mime) = uploaded.get(&sha).ok_or(StatusCode::NOT_FOUND)?;
    let mut headers = axum::http::HeaderMap::new();
    headers.insert(
        axum::http::header::CONTENT_TYPE,
        mime.parse()
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?,
    );
    Ok((headers, Bytes::copy_from_slice(bytes)))
}
pub async fn bounded_bytes(mut response: reqwest::Response) -> Result<Vec<u8>> {
    if !response.status().is_success() {
        bail!("simulator HTTP {}", response.status().as_u16())
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        if bytes.len() + chunk.len() > 16 * 1024 * 1024 {
            bail!("transport image exceeds budget")
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

pub struct SimulatorSender {
    pub media: Arc<MediaService>,
    pub base_url: String,
    pub namespace: String,
    pub run_id: String,
    pub client: reqwest::Client,
}
#[async_trait]
impl Sender for SimulatorSender {
    async fn send(&self, id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        let target = message
            .target_ref
            .as_deref()
            .context("missing send target")?;
        let mut segments = Vec::new();
        for segment in &message.segments {
            segments.push(match segment {
                Segment::Text{text}=>json!({"type":"text","data":{"text":text}}),
                Segment::Face{face_id}=>json!({"type":"face","data":{"id":face_id.to_string()}}),
                Segment::Image{media_id}=>{
                    let upload_started=Instant::now();
                    let (record,bytes)=self.media.bytes_for_target(media_id,target).await?;
                    let key=format!("{}:{}",self.namespace,record.sha256);
                    let (reference,hit)=match self.media.ledger.cache_get("sim-upload",&key).await? {
                        Some(reference)=>(reference.as_str().context("invalid upload cache")?.to_owned(),true),
                        None=>{
                            let response=self.client.post(format!("{}/upload",self.base_url)).json(&json!({"sha256":record.sha256,"mime_type":record.mime_type,"base64":STANDARD.encode(bytes)})).send().await?;
                            if !response.status().is_success(){bail!("upload failed")}
                            let reference=response.json::<Value>().await?["resource_ref"].as_str().context("missing uploaded reference")?.to_owned();
                            if reference!=format!("sim://{}/{}",self.namespace,record.sha256){bail!("invalid uploaded reference")}
                            self.media.ledger.cache_put("sim-upload",&key,json!(reference)).await?;(reference,false)
                        }
                    };
                    self.media.ledger.event(&self.run_id,id,"upload_observation",json!({"media_id":media_id,"target_ref":target,"cache_hit":hit,"elapsed_ms":upload_started.elapsed().as_millis()})).await?;
                    json!({"type":"image","data":{"file":reference}})
                },
            });
        }
        let response = self
            .client
            .post(format!("{}/send", self.base_url))
            .json(&SendRequest {
                action_id: id.into(),
                run_id: self.run_id.clone(),
                target_ref: target.into(),
                message: segments,
            })
            .send()
            .await?;
        if !response.status().is_success() {
            bail!("simulated send receipt unavailable")
        }
        let response: Value = response.json().await?;
        if response["status"] != "ok" {
            bail!("invalid simulator receipt")
        }
        Ok(Receipt {
            transport: "onebot_simulator".into(),
            message_id: response["message_id"]
                .as_str()
                .context("missing simulator message id")?
                .into(),
            message: message.clone(),
        })
    }
}
