//! TypeSafe System One adapter. Only bounded choices enter orchestration.
use crate::{
    protocol::Directory,
    routing::{QuickReplies, Route, RouteContext, Router},
};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, time::Duration};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JevConfig {
    pub base_url: String,
    pub model: String,
    pub api_key_env: String,
    pub timeout_seconds: u64,
    pub min_probability: f64,
    pub min_confidence: f64,
    pub long_context: bool,
    pub prompts: String,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JevPrompts {
    pub route_instructions: String,
    pub generate_criteria: String,
    pub silent_criteria: String,
    pub diagnostics: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct JevResponse {
    pub model: String,
    pub answers: BTreeMap<String, Value>,
    pub usage: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Choice {
    pub choice: String,
    pub probabilities: BTreeMap<String, f64>,
    pub confidence: f64,
}

impl JevResponse {
    pub fn route_choice(&self, expected: &BTreeMap<String, String>) -> Result<Choice> {
        let choice: Choice = serde_json::from_value(
            self.answers
                .get("route")
                .context("missing route answer")?
                .clone(),
        )?;
        if choice.probabilities.len() != expected.len()
            || choice
                .probabilities
                .keys()
                .any(|k| !expected.contains_key(k))
            || !choice.probabilities.contains_key(&choice.choice)
        {
            bail!("Jev returned an unknown choice")
        }
        if !choice.confidence.is_finite()
            || !(0.0..=1.0).contains(&choice.confidence)
            || choice
                .probabilities
                .values()
                .any(|p| !p.is_finite() || !(0.0..=1.0).contains(p))
            || (choice.probabilities.values().sum::<f64>() - 1.0).abs() > 0.02
        {
            bail!("invalid Jev probability distribution")
        }
        Ok(choice)
    }
}

pub struct JevClient {
    config: JevConfig,
    client: reqwest::Client,
    key: String,
}

impl JevClient {
    pub fn new(config: JevConfig) -> Result<Self> {
        if !config.base_url.starts_with("https://")
            || !(1..=180).contains(&config.timeout_seconds)
            || ![config.min_probability, config.min_confidence]
                .iter()
                .all(|p| p.is_finite() && (0.0..=1.0).contains(p))
        {
            bail!("invalid Jev configuration")
        }
        let key = std::env::var(&config.api_key_env)
            .context("Jev API key environment variable is missing")?;
        if key.trim().is_empty() {
            bail!("Jev API key is empty")
        }
        let client = reqwest::Client::builder()
            .user_agent("mutsumi-sync-benchmark/0.2")
            .timeout(Duration::from_secs(config.timeout_seconds))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            config,
            client,
            key,
        })
    }
    pub async fn evaluate(&self, state: &Value, questions: &Value) -> Result<JevResponse> {
        let mut request = self
            .client
            .post(format!(
                "{}/v1/systemone",
                self.config.base_url.trim_end_matches('/')
            ))
            .bearer_auth(&self.key)
            .json(&json!({"model":self.config.model,"state":state,"questions":questions}));
        if self.config.long_context {
            request = request.header("TypeSafe-Danger-Zone-Long-Context", "true")
        }
        let mut response = request
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("Jev transport failure"))?;
        if !response.status().is_success() {
            bail!("Jev returned HTTP {}", response.status().as_u16())
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| anyhow::anyhow!("Jev response read failed"))?
        {
            if bytes.len() + chunk.len() > 1024 * 1024 {
                bail!("Jev response exceeds budget")
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).context("invalid Jev response JSON")
    }
    pub fn accepted(&self, choice: &Choice) -> bool {
        choice
            .probabilities
            .get(&choice.choice)
            .is_some_and(|p| *p >= self.config.min_probability)
            && choice.confidence >= self.config.min_confidence
    }
}

impl JevPrompts {
    pub fn load(path: &Path) -> Result<Self> {
        Ok(serde_yaml::from_str(&std::fs::read_to_string(path)?)?)
    }
    pub fn criteria(&self, replies: &QuickReplies) -> BTreeMap<String, String> {
        let mut c = BTreeMap::from([
            ("generate".into(), self.generate_criteria.clone()),
            ("silent".into(), self.silent_criteria.clone()),
        ]);
        for template in replies.templates.iter().filter(|t| t.enabled) {
            c.insert(
                format!("template__{}", template.id),
                template.description.clone(),
            );
        }
        c
    }
    pub fn questions(&self, replies: &QuickReplies, diagnostics: bool) -> Value {
        let mut questions = if diagnostics {
            self.diagnostics.clone()
        } else {
            BTreeMap::new()
        };
        questions.insert("route".into(),json!({"type":"choice","instructions":self.route_instructions,"criteria":self.criteria(replies)}));
        json!(questions)
    }
}

pub struct JevRouter {
    pub client: JevClient,
    pub prompts: JevPrompts,
    pub replies: QuickReplies,
}

#[async_trait]
impl Router for JevRouter {
    async fn route(&self, context: &RouteContext<'_>) -> Result<Route> {
        let state = routing_state(context);
        let response = self
            .client
            .evaluate(&state, &self.prompts.questions(&self.replies, false))
            .await?;
        let choice = response.route_choice(&self.prompts.criteria(&self.replies))?;
        if !self.client.accepted(&choice) {
            return Ok(Route::Generate);
        }
        Ok(match choice.choice.as_str() {
            "silent" => Route::Silent,
            "generate" => Route::Generate,
            label => {
                let id = label.strip_prefix("template__").context("unknown route")?;
                if !self
                    .replies
                    .templates
                    .iter()
                    .any(|t| t.enabled && t.id == id)
                {
                    bail!("disabled template")
                }
                Route::Template(id.into())
            }
        })
    }
}

pub fn routing_state(context: &RouteContext<'_>) -> Value {
    json!({"current_input":{"text":context.text,"actor":context.actor,"source_target":context.source_target},"history":context.recent_facts,"directory":public_directory(context.directory)})
}

fn public_directory(directory: &Directory) -> Value {
    json!({"targets":directory.targets,"media":directory.media,"face_ids":directory.face_ids})
}
