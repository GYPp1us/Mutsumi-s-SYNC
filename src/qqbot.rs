//! Optional official QQ transport. The SDK bridge never runs an agent or changes NapCat.
use crate::{
    media::MediaService,
    napcat::NotStarted,
    protocol::{Directory, OutgoingMessage, Segment, Target},
    sender::{Receipt, Sender},
};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub base_url: String,
    pub token_env: String,
    pub app_id: String,
    pub bindings: Vec<Binding>,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub scope: String,
    pub conversation_id: String,
    pub label: String,
    pub senders: BTreeMap<String, String>,
    #[serde(default)]
    pub allow_proactive: bool,
}
fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}
impl Binding {
    pub fn target(&self, app: &str) -> String {
        format!(
            "qqbot_{:x}",
            Sha256::digest(format!("{app}:{}:{}", self.scope, self.conversation_id))
        )
    }
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Inbound {
    pub app_id: String,
    pub id: String,
    pub scope: String,
    pub conversation_id: String,
    pub actor_id: String,
    pub occurred_at: String,
    pub segments: Vec<InboundSegment>,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum InboundSegment {
    Text { text: String },
    Image { position: usize },
}
impl Inbound {
    pub fn validate(&self) -> Result<()> {
        if !identifier(&self.app_id)
            || !identifier(&self.id)
            || !identifier(&self.actor_id)
            || !identifier(&self.conversation_id)
            || !matches!(self.scope.as_str(), "c2c" | "group")
            || self.segments.is_empty()
            || self.segments.len() > 64
            || serde_json::to_vec(self)?.len() > 64 * 1024
        {
            bail!("invalid official QQ inbound")
        }
        chrono::DateTime::parse_from_rfc3339(&self.occurred_at)?;
        let positions = self
            .segments
            .iter()
            .filter_map(|s| match s {
                InboundSegment::Image { position } => Some(*position),
                _ => None,
            })
            .collect::<Vec<_>>();
        if positions.len() > 8 || positions.iter().enumerate().any(|(i, p)| i != *p) {
            bail!("invalid official QQ image ordering")
        }
        Ok(())
    }
    pub fn key(&self) -> String {
        format!(
            "{:x}",
            Sha256::digest(format!(
                "{}:{}:{}:{}",
                self.app_id, self.scope, self.conversation_id, self.id
            ))
        )
    }
}
pub struct Client {
    pub config: Config,
    pub enabled: AtomicBool,
    http: reqwest::Client,
    token: String,
}
impl Client {
    pub fn new(config: Config) -> Result<Self> {
        let url = reqwest::Url::parse(&config.base_url)?;
        if url.scheme() != "http"
            || !matches!(
                url.host_str(),
                Some("127.0.0.1" | "localhost" | "[::1]" | "::1")
            )
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            bail!("official QQ bridge requires loopback HTTP")
        }
        if !identifier(&config.app_id) || config.bindings.is_empty() || config.bindings.len() > 128
        {
            bail!("invalid official QQ configuration")
        }
        let mut unique = std::collections::BTreeSet::new();
        for b in &config.bindings {
            if !matches!(b.scope.as_str(), "c2c" | "group")
                || !identifier(&b.conversation_id)
                || b.label.trim().is_empty()
                || b.label.len() > 240
                || b.senders.is_empty()
                || b.senders.len() > 128
                || !unique.insert(b.target(&config.app_id))
                || b.senders.iter().any(|(id, role)| {
                    !identifier(id) || !matches!(role.as_str(), "owner" | "friend" | "stranger")
                })
                || (b.scope == "c2c"
                    && (b.senders.len() != 1 || !b.senders.contains_key(&b.conversation_id)))
            {
                bail!("invalid official QQ identity binding")
            }
        }
        let token =
            std::env::var(&config.token_env).context("official QQ bridge credential missing")?;
        if token.len() < 24 {
            bail!("invalid official QQ bridge credential")
        }
        Ok(Self {
            config,
            token,
            enabled: AtomicBool::new(false),
            http: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(120))
                .build()?,
        })
    }
    pub fn authenticate(&self, value: &str) -> bool {
        let a = Sha256::digest(self.token.as_bytes());
        let b = Sha256::digest(value.as_bytes());
        a.iter().zip(b).fold(0_u8, |x, (a, b)| x | (a ^ b)) == 0
    }
    pub fn binding(&self, inbound: &Inbound) -> Option<(&Binding, &str)> {
        if inbound.app_id != self.config.app_id {
            return None;
        }
        self.config.bindings.iter().find_map(|b| {
            if b.scope == inbound.scope && b.conversation_id == inbound.conversation_id {
                b.senders.get(&inbound.actor_id).map(|r| (b, r.as_str()))
            } else {
                None
            }
        })
    }
    pub fn directory(&self, base: &Directory, include_disabled: bool) -> Directory {
        let mut directory = base.clone();
        directory
            .targets
            .retain(|_, t| t.transport != "qq_official");
        if include_disabled || self.enabled.load(Ordering::Acquire) {
            for b in &self.config.bindings {
                let target = b.target(&self.config.app_id);
                directory.targets.insert(
                    target.clone(),
                    Target {
                        label: b.label.clone(),
                        account: self.config.app_id.clone(),
                        audience: target,
                        transport: "qq_official".into(),
                    },
                );
            }
        }
        directory
    }
    pub async fn status(&self) -> Value {
        let ready = async {
            let r = self
                .http
                .get(format!(
                    "{}/status",
                    self.config.base_url.trim_end_matches('/')
                ))
                .bearer_auth(&self.token)
                .send()
                .await?;
            let v: Value = r.error_for_status()?.json().await?;
            Ok::<_, anyhow::Error>(v["ready"] == true && v["app_id"] == self.config.app_id)
        }
        .await
        .unwrap_or(false);
        json!({"configured":true,"ready":ready,"enabled":self.enabled.load(Ordering::Acquire),"app_id":self.config.app_id,"bindings":self.config.bindings})
    }
    pub async fn image(&self, event_key: &str, position: usize) -> Result<Vec<u8>> {
        let response = self
            .http
            .post(format!(
                "{}/media",
                self.config.base_url.trim_end_matches('/')
            ))
            .bearer_auth(&self.token)
            .json(&json!({"event_key":event_key,"position":position}))
            .send()
            .await?;
        if !response.status().is_success() {
            bail!("official QQ media unavailable")
        }
        let body: Value = response.json().await?;
        let bytes = STANDARD.decode(
            body["base64"]
                .as_str()
                .context("official QQ media body missing")?,
        )?;
        if bytes.len() > 16 * 1024 * 1024 {
            bail!("official QQ media exceeds limit")
        }
        Ok(bytes)
    }
}
pub struct ChannelSender {
    pub fallback: Arc<dyn Sender>,
    pub official: Arc<Client>,
    pub media: Arc<MediaService>,
    pub ledger: crate::ledger::Ledger,
}
#[async_trait]
impl Sender for ChannelSender {
    async fn send(&self, action_id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        let target = message.target_ref.as_deref().context("missing target")?;
        let Some(binding) = self
            .official
            .config
            .bindings
            .iter()
            .find(|b| b.target(&self.official.config.app_id) == target)
        else {
            return self.fallback.send(action_id, message).await;
        };
        if !self.official.enabled.load(Ordering::Acquire) {
            return Err(NotStarted("official QQ transport is disabled").into());
        }
        let mut segments = Vec::new();
        let mut total = 0;
        for part in &message.segments {
            segments.push(match part {
            Segment::Text{text}=>json!({"type":"text","text":text}),
            Segment::Face{..}=>return Err(NotStarted("official QQ native face sending is not supported; use text emoji or an existing image").into()),
            Segment::Image{media_id}=>{let (record,bytes)=self.media.bytes_for_target(media_id,target).await?;total+=bytes.len();if total>16*1024*1024 {return Err(NotStarted("official QQ outbound media exceeds limit").into())}json!({"type":"image","base64":STANDARD.encode(bytes),"mime":record.mime_type})}
        });
        }
        // Derive reply opportunities from the same committed ingress record, not a second write.
        let source = target.to_owned();
        let opportunity: Option<Value> = self.ledger.db(move |db| {
            use rusqlite::OptionalExtension;
            let raw: Option<String> = db.query_row(
                "SELECT e.payload FROM ingress_ids i JOIN events e ON e.seq=i.seq WHERE i.session='live' AND i.source=? ORDER BY e.seq DESC LIMIT 1",
                [source], |r| r.get(0),
            ).optional()?;
            raw.map(|s| Ok(serde_json::from_str::<Value>(&s)?["transport"].clone())).transpose()
        }).await?;
        let reply = opportunity
            .as_ref()
            .filter(|o| {
                o["occurred_at"]
                    .as_str()
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                    .is_some_and(|t| {
                        (0..5 * 60 * 1000).contains(
                            &chrono::Utc::now()
                                .timestamp_millis()
                                .saturating_sub(t.timestamp_millis()),
                        )
                    })
            })
            .and_then(|o| o["id"].as_str());
        if reply.is_none() && !binding.allow_proactive {
            return Err(NotStarted("official QQ reply window expired and proactive messages are not enabled for this target").into());
        }
        let result=self.official.http.post(format!("{}/send",self.official.config.base_url.trim_end_matches('/'))).bearer_auth(&self.official.token).json(&json!({"action_id":action_id,"scope":binding.scope,"target_id":binding.conversation_id,"reply_id":reply,"segments":segments})).send().await.context("official QQ send receipt unknown")?;
        let status = result.status();
        let body: Value = result
            .json()
            .await
            .context("official QQ send receipt invalid")?;
        if !status.is_success() || body["status"] != "succeeded" {
            if body["not_started"] == true {
                return Err(NotStarted("official QQ bridge did not start this send").into());
            }
            bail!("official QQ send result unknown; do not replay")
        }
        let id = body["message_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .context("official QQ receipt has no message id")?;
        Ok(Receipt {
            transport: "qq_official".into(),
            message_id: id.into(),
            message: message.clone(),
        })
    }
}
