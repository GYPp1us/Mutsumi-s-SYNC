use crate::{config::ProviderConfig, protocol::AssistantRound};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::sync::Mutex;

#[async_trait]
pub trait Model: Send + Sync {
    async fn complete(&self, messages: &[Value], tools: &[Value]) -> Result<AssistantRound>;
}

/// Credentials and response bodies never appear in diagnostics.
pub struct OpenAiCompatible {
    client: reqwest::Client,
    config: ProviderConfig,
    api_key: String,
    purpose: String,
    metrics: Mutex<Vec<Value>>,
}

impl OpenAiCompatible {
    pub fn new(config: ProviderConfig) -> Result<Self> {
        Self::for_purpose(config, "main")
    }
    pub fn for_purpose(config: ProviderConfig, purpose: &str) -> Result<Self> {
        let api_key = std::env::var(&config.api_key_env)
            .context("API key environment variable is missing")?;
        Self::with_key(config, purpose, api_key)
    }
    pub fn with_key(config: ProviderConfig, purpose: &str, api_key: String) -> Result<Self> {
        config.validate()?;
        if config.request_options.keys().any(|k| {
            matches!(
                k.as_str(),
                "model" | "messages" | "tools" | "stream" | "max_tokens"
            )
        }) {
            bail!("request_options cannot replace protocol fields")
        }
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
            purpose: purpose.into(),
            metrics: Mutex::new(Vec::new()),
        })
    }
    pub fn metrics(&self) -> Vec<Value> {
        self.metrics.lock().unwrap().clone()
    }
}

#[async_trait]
impl Model for OpenAiCompatible {
    async fn complete(&self, messages: &[Value], tools: &[Value]) -> Result<AssistantRound> {
        let started = std::time::Instant::now();
        let mut body = json!({"model":self.config.model,"messages":messages,"max_tokens":self.config.max_tokens});
        for (key, value) in &self.config.request_options {
            body[key] = value.clone();
        }
        if !tools.is_empty() {
            body["tools"] = json!(tools);
        }
        let image_parts = messages
            .iter()
            .filter_map(|m| m["content"].as_array())
            .flatten()
            .filter(|p| p["type"] == "image_url")
            .count();
        let mut metric = json!({"purpose":self.purpose,"requested_model":self.config.model,"request_bytes":serde_json::to_vec(&body)?.len(),"image_parts":image_parts});
        let result = async {
            let mut response = self
                .client
                .post(self.config.endpoint()?)
                .bearer_auth(&self.api_key)
                .json(&body)
                .send()
                .await
                .map_err(|_| anyhow::anyhow!("provider transport failure (details withheld)"))?;
            let status = response.status();
            metric["http_status"] = json!(status.as_u16());
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
            metric["resolved_model"] = data["model"].clone();
            metric["usage"] = data["usage"].clone();
            metric["finish_reason"] = choice["finish_reason"].clone();
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
        .await;
        metric["elapsed_ms"] = json!(started.elapsed().as_millis());
        metric["status"] = json!(if result.is_ok() { "completed" } else { "error" });
        let mut metrics = self.metrics.lock().unwrap();
        // The persistent ledger owns diagnostics; this convenience view is bounded.
        if metrics.len() >= 4096 {
            metrics.remove(0);
        }
        metrics.push(metric);
        result
    }
}
