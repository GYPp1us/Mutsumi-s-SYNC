use super::{
    WebState,
    routes::{Error, Result, bad, unavailable},
};
use crate::{
    qqbot::{Inbound, InboundSegment},
    scheduler::Source,
};
use axum::{
    Json,
    extract::State,
    http::{HeaderMap, StatusCode},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::{Arc, atomic::Ordering};

pub async fn status(State(state): State<Arc<WebState>>) -> Json<Value> {
    Json(match &state.official {
        Some(c) => c.status().await,
        None => json!({"configured":false,"enabled":false,"ready":false}),
    })
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Enable {
    enabled: bool,
}
pub async fn enable(
    State(state): State<Arc<WebState>>,
    Json(input): Json<Enable>,
) -> Result<Json<Value>> {
    let _control = state
        .control
        .try_lock()
        .map_err(|_| Error(StatusCode::CONFLICT, "请等待当前回合结束后切换"))?;
    let client = state.official.as_ref().ok_or_else(unavailable)?;
    if input.enabled && client.status().await["ready"] != true {
        return Err(Error(StatusCode::CONFLICT, "官方机器人尚未连接"));
    }
    client.enabled.store(input.enabled, Ordering::Release);
    state.update_profile().await.map_err(|_| unavailable())?;
    state
        .ingress
        .ledger
        .event(
            "live",
            "qq-official-control",
            "capabilities_changed",
            json!({"qq_official_enabled":input.enabled}),
        )
        .await
        .map_err(|_| unavailable())?;
    Ok(Json(json!({"enabled":input.enabled})))
}
pub async fn event(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    Json(input): Json<Inbound>,
) -> Result<Json<Value>> {
    let client = state.official.as_ref().ok_or_else(unavailable)?;
    let token = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .unwrap_or("");
    if !client.authenticate(token) {
        return Err(Error(StatusCode::UNAUTHORIZED, "官方通道鉴权失败"));
    }
    input.validate().map_err(|_| bad())?;
    let binding = client.binding(&input);
    let decision = if !client.enabled.load(Ordering::Acquire) {
        "disabled"
    } else if binding.is_none() {
        "unregistered_identity"
    } else {
        "accepted"
    };
    state
        .ingress
        .ledger
        .event(
            "live",
            "qq-official-transport",
            "qq_official_transport",
            json!({"transport":input,"decision":decision}),
        )
        .await
        .map_err(|_| unavailable())?;
    if decision != "accepted" {
        return Ok(Json(json!({"ignored":decision})));
    }
    let (binding, role) = binding.unwrap();
    let target = binding.target(&client.config.app_id);
    let source = Source {
        actor: format!("qqbot:{}:{}:{}", input.app_id, input.scope, input.actor_id),
        kind: "human".into(),
        relationship: role.into(),
        conversation: target.clone(),
        reply_target: Some(target.clone()),
    };
    let mut text = Vec::new();
    let mut images = Vec::new();
    let mut segments = Vec::new();
    for segment in &input.segments {
        match segment {
            InboundSegment::Text { text: content } => {
                text.push(content.clone());
                segments.push(json!({"type":"text","text":content}));
            }
            InboundSegment::Image { position } => {
                images.push(json!({"position":position}));
                segments.push(json!({"type":"image","media_id":null,"description":"图片处理尚未完成；不能猜测内容"}));
            }
        }
    }
    let payload = json!({"event_id":input.id,"source_id":target,"actor":source.actor,"actor_kind":source.kind,"relationship":role,"conversation":target,"source_target":target,"occurred_at":input.occurred_at,"received_at":chrono::Utc::now().to_rfc3339(),"text":text.join("\n"),"segments":segments,"transport":input});
    let key = input.key();
    let jobs = if images.is_empty() {
        vec![]
    } else {
        vec![(
            "vision".into(),
            format!("official:{key}"),
            json!({"source":"official","event_id":input.id,"event_key":key,"target_ref":target,"images":images}),
        )]
    };
    let (seq, fresh) = state
        .ingress
        .ledger
        .ingest_with_jobs("live", &target, &input.id, payload, jobs)
        .await
        .map_err(|_| bad())?;
    if fresh {
        if role == "owner" {
            state
                .set_attention(false)
                .await
                .map_err(|_| unavailable())?;
        }
        state.ingress.changed.notify_one();
    }
    Ok(Json(json!({"accepted":true,"duplicate":!fresh,"seq":seq})))
}
