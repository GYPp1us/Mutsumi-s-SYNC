use crate::protocol::Directory;
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{path::Path, time::Duration};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    pub base_url: String,
    #[serde(default)]
    pub chat_endpoint: Option<String>,
    pub model: String,
    pub api_key_env: String,
    pub timeout_seconds: u64,
    pub max_tokens: u32,
    #[serde(default)]
    pub request_options: std::collections::BTreeMap<String, serde_json::Value>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub provider: ProviderConfig,
    pub database: String,
    pub prompts: String,
    pub quick_replies: String,
    pub max_rounds: usize,
    pub max_corrections: usize,
    pub directory: Directory,
    #[serde(default)]
    pub jev: Option<crate::jev::JevConfig>,
    #[serde(default)]
    pub media: Option<MediaConfig>,
    #[serde(default)]
    pub profile: Option<String>,
    #[serde(default)]
    pub capture_context: bool,
    #[serde(default)]
    pub workspace: Option<crate::workspace::WorkspaceConfig>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaConfig {
    pub root: String,
    pub worker_executable: String,
    pub worker_timeout_seconds: u64,
    pub outbox: String,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prompts {
    #[serde(default)]
    pub persona: String,
    pub runtime: String,
    pub reviewer: String,
    #[serde(default)]
    pub vision: String,
    #[serde(default)]
    pub evaluator: String,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
        if !config.provider.base_url.starts_with("https://") {
            bail!("provider requires HTTPS");
        }
        if !(1..=32).contains(&config.max_rounds)
            || config.max_corrections > 4
            || !(1..=180).contains(&config.provider.timeout_seconds)
            || !(256..=8192).contains(&config.provider.max_tokens)
        {
            bail!("invalid runtime budgets")
        }
        if config.directory.targets.is_empty()
            || config.directory.targets.values().any(|t| {
                !matches!(
                    t.transport.as_str(),
                    "terminal" | "onebot_simulator" | "webui" | "napcat"
                )
            })
        {
            bail!("unsupported target transport");
        }
        Ok(config)
    }
    pub fn prompts(&self) -> Result<Prompts> {
        let p: Prompts =
            serde_yaml::from_str(&std::fs::read_to_string(&self.prompts).context("read prompts")?)?;
        if [&p.runtime, &p.reviewer]
            .iter()
            .any(|x| x.trim().is_empty())
        {
            bail!("runtime and reviewer prompts must be non-empty");
        }
        Ok(p)
    }
}

impl ProviderConfig {
    pub fn endpoint(&self) -> Result<String> {
        let base = reqwest::Url::parse(&self.base_url)?;
        let endpoint = match self.chat_endpoint.as_deref().filter(|s| !s.is_empty()) {
            Some(value) if value.starts_with('/') => base.join(value)?,
            Some(value) => reqwest::Url::parse(value)?,
            None => reqwest::Url::parse(&format!(
                "{}/chat/completions",
                self.base_url.trim_end_matches('/')
            ))?,
        };
        if base.scheme() != "https"
            || endpoint.scheme() != "https"
            || base.origin() != endpoint.origin()
            || !base.username().is_empty()
            || base.password().is_some()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.fragment().is_some()
        {
            bail!("模型端点必须使用同一服务地址的 HTTPS URL，且不能携带用户名或密码")
        }
        Ok(endpoint.to_string())
    }
    pub fn validate(&self) -> Result<()> {
        if self.base_url.len() > 4096 || self.chat_endpoint.as_ref().is_some_and(|s| s.len() > 8192)
        {
            bail!("模型服务地址过长")
        }
        self.endpoint()?;
        if self.model.trim().is_empty()
            || self.model.len() > 200
            || !(1..=180).contains(&self.timeout_seconds)
            || !(256..=8192).contains(&self.max_tokens)
            || serde_json::to_vec(&self.request_options)?.len() > 16384
        {
            bail!("模型名称或请求预算无效")
        }
        if self.request_options.keys().any(|k| {
            matches!(
                k.as_str(),
                "model"
                    | "messages"
                    | "tools"
                    | "stream"
                    | "max_tokens"
                    | "api_key"
                    | "headers"
                    | "authorization"
            )
        }) {
            bail!("高级选项不能覆盖协议和凭据字段")
        }
        Ok(())
    }
    pub fn timeout(&self) -> Duration {
        Duration::from_secs(self.timeout_seconds)
    }
}
