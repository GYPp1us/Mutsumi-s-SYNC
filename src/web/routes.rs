use super::WebState;
use crate::{
    onebot::{InboundSegment, inbound_text, normalize_inbound},
    scheduler::ExternalEvent,
};
use axum::{
    Json, Router,
    body::Body,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{Value, json};
use sha1::Sha1;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{Arc, atomic::Ordering},
    time::{Duration, Instant},
};

struct Error(StatusCode, &'static str);
impl IntoResponse for Error {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
type Result<T> = std::result::Result<T, Error>;
fn bad() -> Error {
    Error(StatusCode::BAD_REQUEST, "请求格式或引用无效")
}
fn unavailable() -> Error {
    Error(
        StatusCode::SERVICE_UNAVAILABLE,
        "服务暂不可用，请查看连接状态",
    )
}

pub fn router(state: Arc<WebState>) -> Router {
    Router::new()
        .route(
            "/",
            get(|| async { Html(include_str!("../../assets/trial.html")) }),
        )
        .route(
            "/app.js",
            get(|| async {
                (
                    [("content-type", "application/javascript; charset=utf-8")],
                    include_str!("../../assets/trial.js"),
                )
            }),
        )
        .route(
            "/health",
            get(|| async { Json(json!({"status":"ok","service":"mutsumi-v4"})) }),
        )
        .route("/api/login", post(login))
        .route("/api/logout", post(logout))
        .route("/api/status", get(status))
        .route("/api/events", get(events))
        .route("/api/messages", post(message))
        .route("/api/uploads", post(upload))
        .route("/api/media/{id}", get(media))
        .route("/api/hooks", get(hooks))
        .route("/api/roles", get(roles).post(save_roles))
        .route("/api/napcat/status", get(napcat_status))
        .route("/api/napcat/qr", get(napcat_qr))
        .route("/api/napcat/refresh", post(napcat_refresh))
        .route("/api/napcat/enabled", post(napcat_enabled))
        .route("/hooks/{source}", post(hook))
        .route("/onebot/events", post(qq_event))
        .layer(DefaultBodyLimit::max(24 * 1024 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), access))
        .with_state(state)
}

fn equal(a: &str, b: &str) -> bool {
    let a = Sha256::digest(a.as_bytes());
    let b = Sha256::digest(b.as_bytes());
    a.iter().zip(b).fold(0_u8, |x, (a, b)| x | (a ^ b)) == 0
}
fn bearer(headers: &HeaderMap) -> Option<&str> {
    headers
        .get("authorization")?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
}
async fn access(State(state): State<Arc<WebState>>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if request.method() == "POST"
        && request
            .headers()
            .get("origin")
            .is_some_and(|v| v.to_str().ok() != Some(&state.settings.public_origin))
    {
        return Error(StatusCode::FORBIDDEN, "来源校验失败").into_response();
    }
    let public = matches!(path, "/" | "/app.js" | "/health" | "/api/login")
        || path.starts_with("/hooks/")
        || path == "/onebot/events";
    if !public {
        let cookie = request
            .headers()
            .get("cookie")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| {
                v.split(';')
                    .find_map(|p| p.trim().strip_prefix("sync_session="))
            });
        let allowed = {
            let mut sessions = state.sessions.lock().unwrap();
            sessions.retain(|_, at| at.elapsed() < Duration::from_secs(12 * 3600));
            cookie.is_some_and(|key| sessions.contains_key(key))
        };
        if !allowed {
            return Error(StatusCode::UNAUTHORIZED, "请先登录").into_response();
        }
        if request.method() == "POST"
            && request
                .headers()
                .get("x-sync-ui")
                .and_then(|v| v.to_str().ok())
                != Some("1")
        {
            return Error(StatusCode::FORBIDDEN, "缺少请求标记").into_response();
        }
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    for (name, value) in [
        ("cache-control", "no-store"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        (
            "content-security-policy",
            "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'",
        ),
    ] {
        headers.insert(name, HeaderValue::from_static(value));
    }
    response
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Login {
    access_key: String,
}
async fn login(State(state): State<Arc<WebState>>, Json(input): Json<Login>) -> Result<Response> {
    {
        let mut attempts = state.login_attempts.lock().unwrap();
        attempts.retain(|at| at.elapsed() < Duration::from_secs(60));
        if attempts.len() >= 10 {
            return Err(Error(StatusCode::TOO_MANY_REQUESTS, "尝试次数过多，请稍候"));
        }
        attempts.push(Instant::now());
    }
    if input.access_key.len() > 512 || !equal(&input.access_key, &state.access_key) {
        return Err(Error(StatusCode::UNAUTHORIZED, "访问口令不正确"));
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    {
        let mut sessions = state.sessions.lock().unwrap();
        if sessions.len() >= 128 {
            sessions.clear();
        }
        sessions.insert(id.clone(), Instant::now());
    }
    let secure = if state.settings.public_origin.starts_with("https://") {
        "; Secure"
    } else {
        ""
    };
    let cookie = format!(
        "sync_session={id}; Path={}; HttpOnly; SameSite=Strict; Max-Age=43200{secure}",
        state.settings.base_path
    );
    Ok(([("set-cookie", cookie)], Json(json!({"ok":true}))).into_response())
}
async fn logout(State(state): State<Arc<WebState>>, headers: HeaderMap) -> Response {
    if let Some(cookie) = headers
        .get("cookie")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            v.split(';')
                .find_map(|p| p.trim().strip_prefix("sync_session="))
        })
    {
        state.sessions.lock().unwrap().remove(cookie);
    }
    (
        [(
            "set-cookie",
            format!(
                "sync_session=; Path={}; HttpOnly; SameSite=Strict; Max-Age=0",
                state.settings.base_path
            ),
        )],
        Json(json!({"ok":true})),
    )
        .into_response()
}
async fn status(State(state): State<Arc<WebState>>) -> Json<Value> {
    let book = state.roles.read().unwrap().clone();
    let directory = state
        .napcat
        .as_ref()
        .map(|c| book.directory(&state.runtime.config.directory, &c.config.bot_id, false))
        .unwrap_or_else(|| state.runtime.config.directory.clone());
    Json(
        json!({"model":state.runtime.config.provider.model,"provider_configured":state.provider_configured,"busy":state.busy.load(Ordering::Acquire),"needs_attention":state.needs_attention.load(Ordering::Acquire),"qq_enabled":state.qq_enabled.load(Ordering::Acquire),"targets":directory.targets,"last_outcome":state.last_outcome.lock().unwrap().clone(),"tools":state.runtime.executor.tools.schemas(),"qq_scope":if book.scope==super::roles::Scope::Owner {"仅接入角色管理中主人账号的私聊；群聊未开启"} else {"接入角色管理中已启用联系人的私聊；群聊未开启"}}),
    )
}
#[derive(Deserialize)]
struct Page {
    #[serde(default)]
    after: i64,
}
async fn events(
    State(state): State<Arc<WebState>>,
    Query(page): Query<Page>,
) -> Result<Json<Value>> {
    if page.after < 0 {
        return Err(bad());
    }
    let events = state
        .ingress
        .ledger
        .stream_page("live", page.after, 200)
        .await
        .map_err(|_| unavailable())?;
    Ok(Json(json!({"events":events})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    id: String,
    text: String,
    #[serde(default)]
    media_ids: Vec<String>,
}
async fn message(
    State(state): State<Arc<WebState>>,
    Json(input): Json<Message>,
) -> Result<Json<Value>> {
    if input.text.trim().is_empty() && input.media_ids.is_empty() {
        return Err(bad());
    }
    let event = ExternalEvent {
        id: input.id,
        occurred_at: chrono::Utc::now().to_rfc3339(),
        text: input.text,
        media: input.media_ids,
        correlation_id: None,
    };
    let (seq, fresh) = state
        .submit("owner_web", &event, true)
        .await
        .map_err(|_| bad())?;
    Ok(Json(json!({"queued":fresh,"seq":seq})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Upload {
    base64: String,
    #[serde(default)]
    allow_qq: bool,
}
async fn upload(
    State(state): State<Arc<WebState>>,
    Json(input): Json<Upload>,
) -> Result<Json<Value>> {
    let bytes = STANDARD.decode(input.base64).map_err(|_| bad())?;
    let mut audience = vec!["web_owner".into()];
    if input.allow_qq {
        if state.napcat.is_none() {
            return Err(bad());
        }
        let book = state.roles.read().unwrap();
        let owner = book
            .contacts
            .values()
            .find(|c| c.role == super::roles::Role::Owner)
            .ok_or_else(bad)?;
        audience.push(book.target(&owner.qq_id));
    }
    let record = state
        .media
        .register(&bytes, "图像尚待观察", audience, "owner-web-upload")
        .await
        .map_err(|_| bad())?;
    Ok(Json(record.reference()))
}
async fn media(State(state): State<Arc<WebState>>, Path(id): Path<String>) -> Result<Response> {
    let (record, bytes) = state
        .media
        .bytes_for_target(&id, "web_owner")
        .await
        .map_err(|_| Error(StatusCode::NOT_FOUND, "图片不可用"))?;
    Ok(Response::builder()
        .header("content-type", record.mime_type)
        .body(Body::from(bytes))
        .unwrap())
}
async fn hooks(State(state): State<Arc<WebState>>) -> Json<Value> {
    Json(
        json!({"sources":state.settings.hooks.iter().map(|(id,hook)|json!({"id":id,"source":hook.source,"token":state.hook_keys[id],"url":format!("{}{}/hooks/{id}",state.settings.public_origin,state.settings.base_path)})).collect::<Vec<_>>()}),
    )
}
async fn roles(State(state): State<Arc<WebState>>) -> Result<Json<Value>> {
    let book = state.roles.read().unwrap().clone();
    let recent=state.ingress.ledger.db(|db| {
        let rows=db.prepare("SELECT payload,at FROM events WHERE session='live' AND kind='qq_transport' ORDER BY seq DESC LIMIT 200")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let mut seen=std::collections::BTreeSet::new();let mut recent=Vec::new();
        for (payload,at) in rows {
            let p:Value=serde_json::from_str(&payload)?;let raw=&p["transport"];
            if raw["post_type"]!="message" {continue}
            let Some(id)=number(&raw["user_id"]) else {continue};
            if seen.insert(id.clone()) {recent.push(json!({"qq_id":id,"nickname":raw["sender"]["nickname"],"message_type":raw["message_type"],"at":at,"decision":p["decision"],"role":p["role"]}));}
        }
        Ok(recent)
    }).await.map_err(|_|unavailable())?;
    Ok(Json(
        json!({"configured":state.napcat.is_some(),"version":book.version,"scope":book.scope,"contacts":book.contacts.values().collect::<Vec<_>>(),"recent":recent}),
    ))
}
async fn save_roles(
    State(state): State<Arc<WebState>>,
    Json(input): Json<super::roles::Update>,
) -> Result<Json<Value>> {
    let client = state.napcat.as_ref().ok_or_else(unavailable)?;
    let _control = state
        .control
        .try_lock()
        .map_err(|_| Error(StatusCode::CONFLICT, "正在处理消息，请稍后保存角色设置"))?;
    // Serialize profile facts with the independent QQ on/off control.
    let _profile_control = state.profile_control.lock().await;
    let old = state.roles.read().unwrap().clone();
    if input.version != old.version {
        return Err(Error(StatusCode::CONFLICT, "角色配置已变化，请重新加载"));
    }
    let next = old.updated(input, &client.config.bot_id).map_err(|_| {
        Error(
            StatusCode::BAD_REQUEST,
            "请检查QQ号和称呼：QQ号唯一且不是机器人，必须且只能保留一个已启用的主人",
        )
    })?;
    state
        .install_roles_profile(&next)
        .await
        .map_err(|_| unavailable())?;
    state
        .media
        .add_targets(next.contacts.keys().map(|id| next.target(id)));
    *state.roles.write().unwrap() = next.clone();
    state
        .ingress
        .ledger
        .event(
            "live",
            "owner-control",
            "roles_changed",
            json!({"actor":"authenticated_web_owner","version":next.version}),
        )
        .await
        .map_err(|_| unavailable())?;
    Ok(Json(json!({"saved":true,"version":next.version})))
}
async fn hook(
    State(state): State<Arc<WebState>>,
    Path(source): Path<String>,
    headers: HeaderMap,
    Json(event): Json<ExternalEvent>,
) -> Result<Json<Value>> {
    let key = state
        .hook_keys
        .get(&source)
        .ok_or(Error(StatusCode::NOT_FOUND, "上报来源不存在"))?;
    if !bearer(&headers).is_some_and(|given| equal(given, key)) {
        return Err(Error(StatusCode::UNAUTHORIZED, "上报凭据无效"));
    }
    let (seq, fresh) = state
        .submit(&source, &event, false)
        .await
        .map_err(|_| bad())?;
    Ok(Json(json!({"accepted":true,"duplicate":!fresh,"seq":seq})))
}
async fn napcat_status(State(state): State<Arc<WebState>>) -> Json<Value> {
    Json(match &state.napcat {
        Some(client) => client.status().await,
        None => json!({"configured":false}),
    })
}
async fn napcat_qr(State(state): State<Arc<WebState>>) -> Result<Response> {
    let client = state.napcat.as_ref().ok_or_else(unavailable)?;
    let svg = client.qr_svg().await.map_err(|_| unavailable())?;
    Ok(([("content-type", "image/svg+xml")], svg).into_response())
}
async fn napcat_refresh(State(state): State<Arc<WebState>>) -> Result<Json<Value>> {
    state
        .napcat
        .as_ref()
        .ok_or_else(unavailable)?
        .web_api("QQLogin/RefreshQRcode", json!({}))
        .await
        .map_err(|_| unavailable())?;
    Ok(Json(json!({"ok":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Enabled {
    enabled: bool,
}
async fn napcat_enabled(
    State(state): State<Arc<WebState>>,
    Json(input): Json<Enabled>,
) -> Result<Json<Value>> {
    let client = state.napcat.as_ref().ok_or_else(unavailable)?;
    if input.enabled {
        let status = client.status().await;
        if status["logged_in"] != true || status["onebot_available"] != true {
            return Err(Error(
                StatusCode::CONFLICT,
                "请先扫码登录，并等待 QQ 接口在线",
            ));
        }
    }
    state.qq_enabled.store(input.enabled, Ordering::Release);
    state.update_profile().await.map_err(|_| unavailable())?;
    state
        .ingress
        .ledger
        .event(
            "live",
            "owner-control",
            "qq_control",
            json!({"enabled":input.enabled,"actor":"authenticated_web_owner"}),
        )
        .await
        .map_err(|_| unavailable())?;
    Ok(Json(json!({"enabled":input.enabled})))
}

fn number(value: &Value) -> Option<String> {
    value.as_u64().map(|v| v.to_string()).or_else(|| {
        value
            .as_str()
            .filter(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()))
            .map(str::to_owned)
    })
}
async fn qq_event(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>> {
    if !onebot_signature(&state.onebot_hook_key, &headers, &body) {
        let _ = state
            .ingress
            .ledger
            .event(
                "live",
                "onebot-auth",
                "qq_callback_rejected",
                json!({"reason":"invalid_or_missing_hmac_sha1_signature"}),
            )
            .await;
        return Err(Error(StatusCode::UNAUTHORIZED, "OneBot 凭据无效"));
    }
    let raw: Value = serde_json::from_slice(&body).map_err(|_| bad())?;
    let client = state.napcat.as_ref().ok_or_else(unavailable)?;
    if number(&raw["self_id"]).as_deref() != Some(&client.config.bot_id) {
        return Err(bad());
    }
    let book = state.roles.read().unwrap().clone();
    let contact = number(&raw["user_id"]).and_then(|id| book.contacts.get(&id).cloned());
    let decision = if raw["post_type"] != "message" {
        "not_message"
    } else if raw["message_type"] != "private" {
        "group_not_enabled"
    } else if !state.qq_enabled.load(Ordering::Acquire) {
        "qq_disabled"
    } else if contact.is_none() {
        "unregistered_stranger"
    } else if !book.allows(contact.as_ref().unwrap()) {
        "role_or_scope_disabled"
    } else {
        "accepted"
    };
    state.ingress.ledger.event("live","onebot-transport","qq_transport",json!({"transport":raw,"decision":decision,"role":contact.as_ref().map(|c|c.role.relationship()).unwrap_or("stranger"),"role_version":book.version})).await.map_err(|_|unavailable())?;
    if decision != "accepted" {
        return Ok(Json(json!({"ignored":decision})));
    }
    let contact = contact.unwrap();
    let source = book.source(&contact);
    let target = book.target(&contact.qq_id);
    let id = raw["message_id"]
        .as_i64()
        .map(|id| id.to_string())
        .or_else(|| raw["message_id"].as_str().map(str::to_owned))
        .ok_or_else(bad)?;
    let array = raw["message"]
        .as_array()
        .filter(|a| a.len() <= 64)
        .ok_or_else(bad)?;
    let mut segments = Vec::new();
    let mut images = Vec::new();
    for part in array {
        if part["type"] == "image" {
            images.push(part["data"].clone());
        }
        let normalized = normalize_inbound(std::slice::from_ref(part), &BTreeMap::new())
            .unwrap_or_else(|_| {
                vec![InboundSegment::Text {
                    text: format!(
                        "[暂不支持的消息段：{}]",
                        part["type"].as_str().unwrap_or("unknown")
                    ),
                }]
            });
        segments.extend(normalized);
    }
    let occurred = raw["time"]
        .as_i64()
        .and_then(|s| chrono::DateTime::from_timestamp(s, 0))
        .unwrap_or_else(chrono::Utc::now);
    let payload = json!({"event_id":id,"source_id":target,"actor":source.actor,"actor_kind":source.kind,"relationship":source.relationship,"conversation":source.conversation,"source_target":source.reply_target,"role_version":book.version,"occurred_at":occurred.to_rfc3339(),"received_at":chrono::Utc::now().to_rfc3339(),"text":inbound_text(&segments),"segments":segments,"transport":raw});
    let (seq, fresh) = state
        .ingress
        .ledger
        .ingest("live", &target, &id, payload)
        .await
        .map_err(|_| bad())?;
    if fresh {
        if contact.role == super::roles::Role::Owner {
            state
                .set_attention(false)
                .await
                .map_err(|_| unavailable())?;
        }
        state.ingress.changed.notify_one();
        if !images.is_empty() {
            let state = state.clone();
            let task_state = state.clone();
            state.spawn(async move {
                let mut observations=Vec::new();
                for (position,image) in images.into_iter().enumerate() {
                    let result=async {
                        let client=task_state.napcat.as_ref().unwrap();let file=image["file"].as_str().unwrap_or("");
                        let reference = if file.is_empty() {
                            image["url"].as_str().ok_or_else(||anyhow::anyhow!("missing image reference"))?
                        } else { file };
                        let key=format!("{}:{reference}",client.config.bot_id);
                        let cached=task_state.ingress.ledger.cache_get("qq-image",&key).await?;
                        let bytes=if let Some(id)=cached.and_then(|v|v.as_str().map(str::to_owned)) {task_state.media.bytes_for_target(&id,&target).await?.1}else{client.image_bytes(file,image["url"].as_str()).await?};
                        let record=task_state.media.register(&bytes,"图像尚待观察",vec![target.clone(),"web_owner".into()],"qq-private-ingress").await?;
                        task_state.ingress.ledger.cache_put("qq-image",&key,json!(record.id)).await?;
                        let (record,description,hit)=task_state.vision.ingest(bytes,record.allowed_targets).await?;
                        Ok::<_,anyhow::Error>(json!({"media":record.reference(),"visual_observation":description,"vision_cache_hit":hit}))
                    }.await;
                    observations.push(match result{Ok(data)=>json!({"position":position,"status":"available","data":data}),Err(_)=>json!({"position":position,"status":"unavailable","reason":"QQ image download or observation unavailable; do not guess"})});
                }
                let _=task_state.ingress.observed_media(&id,json!(observations)).await;
            });
        }
    }
    Ok(Json(json!({"accepted":true,"duplicate":!fresh,"seq":seq})))
}

fn onebot_signature(key: &str, headers: &HeaderMap, body: &[u8]) -> bool {
    let Some(hex) = headers
        .get("x-signature")
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("sha1="))
    else {
        return false;
    };
    if hex.len() != 40 || !hex.is_ascii() {
        return false;
    }
    let Ok(signature) = (0..40)
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16))
        .collect::<std::result::Result<Vec<_>, _>>()
    else {
        return false;
    };
    let Ok(mut mac) = Hmac::<Sha1>::new_from_slice(key.as_bytes()) else {
        return false;
    };
    mac.update(body);
    mac.verify_slice(&signature).is_ok()
}
