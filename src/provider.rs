use crate::{config::ProviderConfig, protocol::AssistantRound};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde_json::{Value, json};

#[async_trait]
pub trait Model: Send + Sync {
    async fn complete(&self, messages: &[Value], tools: &[Value]) -> Result<AssistantRound>;
}

/// Credentials and response bodies never appear in diagnostics.
pub struct OpenAiCompatible {
    client: reqwest::Client,
    config: ProviderConfig,
    api_key: String,
}

impl OpenAiCompatible {
    pub fn new(config: ProviderConfig) -> Result<Self> {
        let api_key = std::env::var(&config.api_key_env)
            .context("API key environment variable is missing")?;
        if api_key.trim().is_empty() {
            bail!("API key is empty")
        }
        let client = reqwest::Client::builder()
            .user_agent("mutsumi-sync-v4-prototype/0.1")
            .timeout(config.timeout())
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            client,
            config,
            api_key,
        })
    }
}

#[async_trait]
impl Model for OpenAiCompatible {
    async fn complete(&self, messages: &[Value], tools: &[Value]) -> Result<AssistantRound> {
        let mut body = json!({"model":self.config.model,"messages":messages,"max_tokens":self.config.max_tokens});
        if !tools.is_empty() {
            body["tools"] = json!(tools);
        }
        let mut response = self
            .client
            .post(format!(
                "{}/chat/completions",
                self.config.base_url.trim_end_matches('/')
            ))
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("provider transport failure (details withheld)"))?;
        let status = response.status();
        if !status.is_success() {
            bail!("provider returned HTTP {}", status.as_u16())
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| anyhow::anyhow!("provider response read failed"))?
        {
            if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                bail!("provider response exceeds limit")
            }
            bytes.extend_from_slice(&chunk);
        }
        let data: Value = serde_json::from_slice(&bytes).context("invalid provider JSON")?;
        let choice = data
            .get("choices")
            .and_then(|x| x.get(0))
            .context("provider returned no choices")?;
        if !matches!(
            choice["finish_reason"].as_str(),
            Some("stop" | "tool_calls")
        ) {
            bail!("incomplete or unsupported provider finish_reason");
        }
        let round: AssistantRound = serde_json::from_value(choice["message"].clone())
            .context("invalid assistant message")?;
        let mut ids = std::collections::HashSet::new();
        if round.tool_calls.len() > 8
            || round
                .tool_calls
                .iter()
                .any(|c| c.id.is_empty() || c.kind != "function" || !ids.insert(&c.id))
        {
            bail!("invalid native tool call batch");
        }
        Ok(round)
    }
}
