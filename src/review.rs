use crate::{
    protocol::{Action, Directory},
    provider::Model,
};
use anyhow::{Result, bail};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::sync::Arc;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Allow,
    Reject,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Verdict {
    pub decision: Decision,
    pub reason: String,
}

#[derive(Serialize)]
pub struct ReviewContext<'a> {
    pub revision: i64,
    pub source_target: &'a str,
    pub actor: &'a str,
    pub user_request: &'a str,
    pub directory: &'a Directory,
    pub tool_schema: Option<Value>,
    pub observed_facts: Vec<Value>,
    pub proposed_action: &'a Action,
}

#[async_trait]
pub trait Reviewer: Send + Sync {
    async fn review(&self, evidence: &ReviewContext<'_>) -> Result<Verdict>;
}

pub struct ModelReviewer {
    pub model: Arc<dyn Model>,
    pub system: String,
}

#[async_trait]
impl Reviewer for ModelReviewer {
    async fn review(&self, evidence: &ReviewContext<'_>) -> Result<Verdict> {
        // A fresh request: no agent trace, scratchpad, tools, or recursive reviewer.
        let round = self
            .model
            .complete(
                &[
                    json!({"role":"system","content":self.system}),
                    json!({"role":"user","content":serde_json::to_string(evidence)?}),
                ],
                &[],
            )
            .await?;
        if !round.tool_calls.is_empty() {
            bail!("reviewer attempted a tool call")
        }
        let v: Verdict = serde_json::from_str(round.content.as_deref().unwrap_or(""))?;
        if v.reason.len() > 1024 {
            bail!("review reason exceeds limit")
        }
        Ok(v)
    }
}
