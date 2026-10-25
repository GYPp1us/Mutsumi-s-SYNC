//! Durable multi-source mailbox. It schedules attention, never a business workflow.
use crate::{
    ledger::Ledger,
    runtime::{Runtime, TurnOutcome},
};
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::Notify;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub actor: String,
    pub kind: String,
    pub relationship: String,
    pub conversation: String,
    pub reply_target: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExternalEvent {
    pub id: String,
    pub occurred_at: String,
    pub text: String,
    #[serde(default)]
    pub media: Vec<String>,
    #[serde(default)]
    pub correlation_id: Option<String>,
}

pub struct Ingress {
    pub ledger: Ledger,
    pub session: String,
    pub sources: BTreeMap<String, Source>,
    pub changed: Arc<Notify>,
}
impl Ingress {
    pub async fn receive(&self, source_id: &str, event: &ExternalEvent) -> Result<(i64, bool)> {
        let source = self
            .sources
            .get(source_id)
            .ok_or_else(|| anyhow::anyhow!("unregistered ingress source"))?;
        if event.id.is_empty()
            || event.id.len() > 128
            || event.text.len() > 32 * 1024
            || event.media.len() > 8
            || chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_err()
        {
            bail!("invalid ingress event")
        }
        let received = chrono::Utc::now().to_rfc3339();
        let mut segments = vec![json!({"type":"text","text":event.text})];
        for resource in &event.media {
            segments.push(json!({"type":"image","media_id":null,"description":"图片处理尚未完成，不能猜测内容","source_ref":resource}));
        }
        let payload = json!({"event_id":event.id,"source_id":source_id,"actor":source.actor,"actor_kind":source.kind,"relationship":source.relationship,"conversation":source.conversation,"source_target":source.reply_target,"occurred_at":event.occurred_at,"received_at":received,"text":event.text,"segments":segments,"correlation_id":event.correlation_id,"media_status":if event.media.is_empty(){"none"}else{"pending"},"transport":event});
        let result = self
            .ledger
            .ingest(&self.session, source_id, &event.id, payload)
            .await?;
        if result.1 {
            self.changed.notify_one();
        }
        Ok(result)
    }
    pub async fn observed_media(&self, event_id: &str, observations: Value) -> Result<()> {
        self.ledger.event(&self.session,event_id,"media_observation",json!({"event_id":event_id,"observations":observations,"observed_at":chrono::Utc::now().to_rfc3339()})).await?;
        self.changed.notify_one();
        Ok(())
    }
}

pub struct Scheduler {
    pub runtime: Arc<Runtime>,
    pub ingress: Arc<Ingress>,
}
impl Scheduler {
    pub async fn step(&self) -> Result<TurnOutcome> {
        let outcome = self.runtime.run_pending(&self.ingress.session).await?;
        if matches!(outcome.status.as_str(), "completed" | "silent") {
            let through = self
                .ingress
                .ledger
                .projected_through(&self.ingress.session)
                .await?;
            self.ingress
                .ledger
                .checkpoint(&self.ingress.session, through)
                .await?;
        }
        Ok(outcome)
    }
    pub async fn drain(&self, producers_done: Arc<AtomicBool>) -> Result<Vec<TurnOutcome>> {
        let mut outcomes = Vec::new();
        loop {
            let pending = self.ingress.ledger.pending(&self.ingress.session).await?;
            if pending.is_empty() {
                if producers_done.load(Ordering::Acquire) {
                    break;
                }
                tokio::select! {_=self.ingress.changed.notified()=>(),_=self.runtime.executor.cancel.cancelled()=>break};
                continue;
            }
            // A short fixed coalescing window keeps bursts together without task-specific rules.
            tokio::time::sleep(Duration::from_millis(180)).await;
            let outcome = self.step().await?;
            let completed = matches!(outcome.status.as_str(), "completed" | "silent");
            outcomes.push(outcome);
            if !completed {
                break;
            } // unresolved/unknown actions need attention, never an automatic replay
        }
        Ok(outcomes)
    }
}
