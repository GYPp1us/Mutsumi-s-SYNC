use super::{
    WebState,
    routes::{Error, Result, bad, unavailable},
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::Arc;

pub async fn index(State(state): State<Arc<WebState>>) -> Result<Json<Value>> {
    let knowledge = state
        .runtime
        .executor
        .tools
        .knowledge
        .as_ref()
        .ok_or_else(unavailable)?;
    Ok(Json(
        json!({"notes":state.ingress.ledger.notes("live",true).await.map_err(|_|unavailable())?,"skill_catalog":knowledge.skills.catalog().await.map_err(|_|unavailable())?,"deliveries":state.deliveries.summary().await.map_err(|_|unavailable())?}),
    ))
}
pub async fn save_note(
    State(state): State<Arc<WebState>>,
    Json(input): Json<crate::memory::NoteInput>,
) -> Result<Json<Value>> {
    input.validate().map_err(|_| bad())?;
    let result = state
        .ingress
        .ledger
        .save_note("live", &state.settings.owner_source.actor, input)
        .await
        .map_err(|_| {
            Error(
                axum::http::StatusCode::CONFLICT,
                "笔记版本或历史依据无效，请重新加载后保存",
            )
        })?;
    Ok(Json(result))
}
#[derive(Deserialize)]
pub struct Resource {
    path: String,
    revision: String,
}
pub async fn read_skill(
    State(state): State<Arc<WebState>>,
    Path(name): Path<String>,
    Query(resource): Query<Resource>,
) -> Result<Json<Value>> {
    let knowledge = state
        .runtime
        .executor
        .tools
        .knowledge
        .as_ref()
        .ok_or_else(unavailable)?;
    Ok(Json(
        knowledge
            .skills
            .read(&name, &resource.path, &resource.revision)
            .await
            .map_err(|_| bad())?,
    ))
}
pub async fn save_skill(
    State(state): State<Arc<WebState>>,
    Path(name): Path<String>,
    Json(edit): Json<crate::skills::Edit>,
) -> Result<Json<Value>> {
    let knowledge = state
        .runtime
        .executor
        .tools
        .knowledge
        .as_ref()
        .ok_or_else(unavailable)?;
    let result = knowledge.skills.edit(&name, edit).await.map_err(|_| {
        Error(
            axum::http::StatusCode::CONFLICT,
            "技能内容、路径或版本无效；草稿保留，请重新加载检查",
        )
    })?;
    state.ingress.ledger.event("live","skills","knowledge_changed",json!({"skill":name,"revision":result["revision"],"actor":state.settings.owner_source.actor})).await.map_err(|_|unavailable())?;
    Ok(Json(result))
}
#[derive(Deserialize)]
pub struct HistoryQuery {
    #[serde(default)]
    query: String,
    #[serde(default)]
    after: i64,
}
pub async fn history(
    State(state): State<Arc<WebState>>,
    Query(q): Query<HistoryQuery>,
) -> Result<Json<Value>> {
    Ok(Json(
        state
            .ingress
            .ledger
            .recall("live", &q.query, q.after, 20)
            .await
            .map_err(|_| bad())?,
    ))
}
