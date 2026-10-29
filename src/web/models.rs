//! Atomic private model settings. Only redacted views enter HTTP responses or facts.
use crate::{
    config::ProviderConfig,
    ledger::Ledger,
    media::MediaService,
    provider::{Model, OpenAiCompatible},
    vision::VisionCache,
};
use anyhow::{Result, bail};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{Arc, RwLock},
};

#[derive(Clone, Deserialize, Serialize)]
struct PrivateSettings {
    version: u64,
    provider: ProviderConfig,
    console_url: String,
    api_key: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Update {
    pub version: u64,
    pub base_url: String,
    pub chat_endpoint: Option<String>,
    pub model: String,
    pub console_url: String,
    pub timeout_seconds: u64,
    pub max_tokens: u32,
    pub request_options: BTreeMap<String, Value>,
    #[serde(default)]
    pub api_key: String,
}
pub struct Active {
    settings: PrivateSettings,
    pub main: Arc<dyn Model>,
    pub reviewer: Arc<dyn Model>,
    pub vision: Arc<VisionCache>,
}
impl Active {
    pub fn config(&self) -> &ProviderConfig {
        &self.settings.provider
    }
    pub fn version(&self) -> u64 {
        self.settings.version
    }
    pub fn configured(&self) -> bool {
        !self.settings.api_key.is_empty()
    }
    pub fn metadata(&self) -> Value {
        json!({"version":self.version(),"model":self.config().model})
    }
    pub fn public(&self) -> Value {
        let s = &self.settings;
        json!({"version":s.version,"base_url":s.provider.base_url,"chat_endpoint":s.provider.chat_endpoint,"effective_endpoint":s.provider.endpoint().ok(),"model":s.provider.model,"console_url":s.console_url,"timeout_seconds":s.provider.timeout_seconds,"max_tokens":s.provider.max_tokens,"request_options":s.provider.request_options,"key_configured":self.configured()})
    }
}
pub struct Models {
    pub ledger: Ledger,
    current: RwLock<Arc<Active>>,
    media: Arc<MediaService>,
    vision_prompt: String,
}
impl Models {
    pub async fn load(
        ledger: Ledger,
        base: ProviderConfig,
        media: Arc<MediaService>,
        vision_prompt: String,
    ) -> Result<Self> {
        let saved: Option<String> = ledger
            .db(|db| {
                Ok(db
                    .query_row(
                        "SELECT value FROM private_settings WHERE name='model'",
                        [],
                        |r| r.get(0),
                    )
                    .optional()?)
            })
            .await?;
        let settings = match saved {
            Some(s) => serde_json::from_str(&s)?,
            None => PrivateSettings {
                version: 0,
                api_key: std::env::var(&base.api_key_env).unwrap_or_default(),
                provider: base,
                console_url: String::new(),
            },
        };
        let active = Self::build(settings, &media, &vision_prompt)?;
        Ok(Self {
            ledger,
            current: RwLock::new(active),
            media,
            vision_prompt,
        })
    }
    fn build(
        settings: PrivateSettings,
        media: &Arc<MediaService>,
        vision_prompt: &str,
    ) -> Result<Arc<Active>> {
        settings.provider.validate()?;
        if settings.console_url.len() > 4096 {
            bail!("服务商链接过长")
        }
        if !settings.console_url.is_empty() {
            let url = reqwest::Url::parse(&settings.console_url)?;
            if url.scheme() != "https" || !url.username().is_empty() || url.password().is_some() {
                bail!("服务商链接必须是 HTTPS URL")
            }
        }
        let model = |purpose: &str| -> Result<Arc<dyn Model>> {
            if settings.api_key.is_empty() {
                Ok(Arc::new(UnconfiguredModel))
            } else {
                Ok(Arc::new(OpenAiCompatible::with_key(
                    settings.provider.clone(),
                    purpose,
                    settings.api_key.clone(),
                )?))
            }
        };
        let main = model("main")?;
        let reviewer = model("review")?;
        let observer = model("vision")?;
        let fingerprint = format!(
            "{}:{:x}",
            settings.provider.model,
            Sha256::digest(serde_json::to_vec(&settings.provider)?)
        );
        let vision = Arc::new(VisionCache::new(
            media.clone(),
            observer,
            fingerprint,
            vision_prompt.into(),
        )?);
        Ok(Arc::new(Active {
            settings,
            main,
            reviewer,
            vision,
        }))
    }
    pub fn current(&self) -> Arc<Active> {
        self.current.read().unwrap().clone()
    }
    pub fn candidate(&self, update: Update) -> Result<Arc<Active>> {
        let old = self.current();
        if update.version != old.version() {
            bail!("模型配置已变化，请重新加载")
        }
        let provider = ProviderConfig {
            base_url: update.base_url,
            chat_endpoint: update.chat_endpoint,
            model: update.model,
            api_key_env: old.config().api_key_env.clone(),
            timeout_seconds: update.timeout_seconds,
            max_tokens: update.max_tokens,
            request_options: update.request_options,
        };
        if reqwest::Url::parse(&provider.base_url)?.origin()
            != reqwest::Url::parse(&old.config().base_url)?.origin()
            && update.api_key.is_empty()
        {
            bail!("更换服务地址时请填写新的 API Key")
        }
        let key = if update.api_key.is_empty() {
            old.settings.api_key.clone()
        } else {
            update.api_key
        };
        if key.len() > 8192 || key.bytes().any(|c| !(33..=126).contains(&c)) {
            bail!("API Key 格式无效")
        }
        Self::build(
            PrivateSettings {
                version: old
                    .version()
                    .checked_add(1)
                    .ok_or_else(|| anyhow::anyhow!("配置版本溢出"))?,
                provider,
                api_key: key,
                console_url: update.console_url,
            },
            &self.media,
            &self.vision_prompt,
        )
    }
    pub async fn commit(&self, candidate: Arc<Active>, profile: Value) -> Result<()> {
        let private = serde_json::to_string(&candidate.settings)?;
        let metadata = candidate.metadata();
        self.ledger.db(move|db| {
            let tx=db.transaction()?;
            tx.execute("INSERT INTO private_settings VALUES('model',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",[private])?;
            tx.execute("INSERT INTO cache VALUES('runtime','profile',?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",[profile.to_string()])?;
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('runtime','model-settings','profile_changed',?)",[profile.to_string()])?;
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('live','model-settings','model_settings_changed',?)",params![metadata.to_string()])?;
            tx.commit()?;Ok(())
        }).await?;
        *self.current.write().unwrap() = candidate;
        Ok(())
    }
    pub async fn test(&self, update: Update) -> Result<Value> {
        let active = self.candidate(update)?;
        if !active.configured() {
            bail!("尚未配置 API Key")
        }
        let mut config = active.config().clone();
        config.max_tokens = 256;
        let model =
            OpenAiCompatible::with_key(config, "connection-test", active.settings.api_key.clone())?;
        let start = std::time::Instant::now();
        model
            .complete(
                &[json!({"role":"user","content":"Return a short JSON object with ok=true."})],
                &[],
            )
            .await?;
        Ok(
            json!({"ok":true,"model":active.config().model,"elapsed_ms":start.elapsed().as_millis(),"resolved_model":model.metrics().last().and_then(|m|m.get("resolved_model")).cloned()}),
        )
    }
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
