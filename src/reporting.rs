//! A registered report is a host-bound source, not a workflow or human identity.
use crate::ledger::Ledger;
use anyhow::{Result, bail};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use tokio::sync::Notify;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Definition {
    pub event: String,
    pub description: String,
    pub source_kind: String,
}
impl Definition {
    pub fn validate(&self) -> Result<()> {
        if self.event.is_empty()
            || self.event.len() > 96
            || !self
                .event
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
            || self.description.trim().is_empty()
            || self.description.len() > 2048
            || !matches!(
                self.source_kind.as_str(),
                "service" | "email" | "child_agent"
            )
        {
            bail!("invalid report event definition")
        }
        Ok(())
    }
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Submission {
    pub event: String,
    pub id: String,
    pub content: Value,
    #[serde(default)]
    pub occurred_at: Option<String>,
    #[serde(default)]
    pub correlation_id: Option<String>,
}
#[derive(Debug)]
pub enum Rejection {
    Unknown,
    Unauthorized,
    Disabled,
}
impl std::fmt::Display for Rejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "report admission rejected: {self:?}")
    }
}
impl std::error::Error for Rejection {}

pub struct EventRegistry {
    pub ledger: Ledger,
    pub endpoint: String,
    changed: Arc<Notify>,
}
impl EventRegistry {
    pub fn new(ledger: Ledger, endpoint: String, changed: Arc<Notify>) -> Self {
        Self {
            ledger,
            endpoint,
            changed,
        }
    }
    pub async fn catalog(&self, credentials: bool) -> Result<Vec<Value>> {
        self.ledger.db(move|db| {
            let rows=db.prepare("SELECT name,description,source_kind,enabled,token FROM report_events ORDER BY name")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,bool>(3)?,r.get::<_,String>(4)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
            Ok(rows.into_iter().map(|(event,description,source_kind,enabled,token)|{let mut v=json!({"event":event,"description":description,"source_kind":source_kind,"enabled":enabled});if credentials {v["token"]=json!(token)}v}).collect())
        }).await
    }
    pub async fn register(&self, definition: Definition) -> Result<Value> {
        definition.validate()?;
        let d = definition.clone();
        let enabled=self.ledger.db(move|db| {
            let tx=db.transaction()?;
            let old:Option<(String,String,bool)>=tx.query_row("SELECT description,source_kind,enabled FROM report_events WHERE name=?",[&d.event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
            let enabled=if let Some((description,kind,enabled))=old {
                if description!=d.description || kind!=d.source_kind {bail!("event already exists with another definition; choose another name")}
                enabled
            } else {
                let count:i64=tx.query_row("SELECT COUNT(*) FROM report_events",[],|r|r.get(0))?;
                if count>=256 {bail!("report registry full")}
                let token=format!("{}{}",uuid::Uuid::new_v4().simple(),uuid::Uuid::new_v4().simple());
                tx.execute("INSERT INTO report_events VALUES(?,?,?,?,1)",params![d.event,d.description,d.source_kind,token])?;
                tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('live','report-registry','capabilities_changed',?)",[json!({"report_event":d,"operation":"registered","actor":"action_model"}).to_string()])?;
                true
            };
            tx.commit()?;Ok(enabled)
        }).await?;
        // Credentials stay in the authenticated management UI, outside model traces.
        Ok(
            json!({"registered":true,"enabled":enabled,"definition":definition,"endpoint":self.endpoint,"credential_location":"WebUI 外部上报页","request_example":{"event":definition.event,"id":"unique-delivery-id","content":{"message":"一条新的观察"}}}),
        )
    }
    pub async fn set_enabled(&self, event: String, enabled: bool) -> Result<Value> {
        let e = event.clone();
        self.ledger.db(move|db| {
            let tx=db.transaction()?;
            if tx.execute("UPDATE report_events SET enabled=? WHERE name=?",params![enabled,e])?!=1 {bail!("unknown registered event")}
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('live','report-registry','capabilities_changed',?)",[json!({"event":e,"enabled":enabled,"actor":"action_model"}).to_string()])?;
            tx.commit()?;Ok(())
        }).await?;
        Ok(json!({"event":event,"enabled":enabled}))
    }
    pub async fn receive(&self, submission: Submission, token: &str) -> Result<(i64, bool)> {
        if submission.event.is_empty()
            || submission.event.len() > 96
            || submission.id.is_empty()
            || submission.id.len() > 128
            || serde_json::to_vec(&submission.content)?.len() > 32768
            || submission
                .correlation_id
                .as_ref()
                .is_some_and(|s| s.len() > 128)
        {
            bail!("invalid report content or id")
        }
        let occurred = match &submission.occurred_at {
            Some(s) => {
                chrono::DateTime::parse_from_rfc3339(s)?;
                s.clone()
            }
            None => chrono::Utc::now().to_rfc3339(),
        };
        let token = token.to_owned();
        let result=self.ledger.db(move|db| {
            let tx=db.transaction()?;
            let record=tx.query_row("SELECT description,source_kind,token,enabled FROM report_events WHERE name=?",[&submission.event],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,bool>(3)?))).optional()?;
            let Some((description,kind,secret,enabled))=record else {return Err(Rejection::Unknown.into())};
            let a=Sha256::digest(secret.as_bytes());let b=Sha256::digest(token.as_bytes());
            if a.iter().zip(b).fold(0u8,|x,(a,b)|x|(a^b))!=0 {return Err(Rejection::Unauthorized.into())}
            if !enabled {return Err(Rejection::Disabled.into())}
            let source=format!("report:{}",submission.event);
            let text=match &submission.content {Value::String(s)=>s.clone(),v=>serde_json::to_string(v)?};
            let payload=json!({"event_id":submission.id,"source_id":source,"actor":source,"actor_kind":kind,"relationship":"registered_report","conversation":source,"source_target":null,"occurred_at":occurred,"received_at":chrono::Utc::now().to_rfc3339(),"text":text,"segments":[{"type":"text","text":text}],"report_event":{"event":submission.event,"description":description},"correlation_id":submission.correlation_id,"transport":submission});
            let result=Ledger::ingest_transaction(&tx,"live",&source,&submission.id,&payload)?;
            tx.commit()?;Ok(result)
        }).await?;
        if result.1 {
            self.changed.notify_one()
        }
        Ok(result)
    }
}
