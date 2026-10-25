use crate::protocol::{Directory, Envelope, OutgoingMessage, Segment};
use anyhow::{Result, bail};
use async_trait::async_trait;
use serde::Deserialize;
use serde_json::Value;
use std::{collections::HashSet, path::Path};

pub enum Route {
    Generate,
    Template(String),
    Silent,
}

#[async_trait]
pub trait Router: Send + Sync {
    async fn route(&self, context: &RouteContext<'_>) -> Result<Route>;
}

pub struct RouteContext<'a> {
    pub text: &'a str,
    pub actor: &'a str,
    pub source_target: &'a str,
    pub directory: &'a Directory,
    pub recent_facts: Vec<Value>,
}

/// Until a measured Jev adapter is enabled, every input reaches the main model.
pub struct MainModelRouter;
#[async_trait]
impl Router for MainModelRouter {
    async fn route(&self, _context: &RouteContext<'_>) -> Result<Route> {
        Ok(Route::Generate)
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuickReplies {
    pub version: u32,
    pub templates: Vec<Template>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub id: String,
    pub category: String,
    pub enabled: bool,
    pub description: String,
    pub target_binding: String,
    pub messages: Vec<TemplateMessage>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TemplateMessage {
    pub segments: Vec<Segment>,
}

impl QuickReplies {
    pub fn load(path: &Path) -> Result<Self> {
        let replies: Self = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
        let mut ids = HashSet::new();
        if replies.version != 1
            || replies.templates.len() > 256
            || replies.templates.iter().any(|t| {
                t.target_binding != "source_conversation"
                    || t.messages.is_empty()
                    || t.messages.len() > 16
                    || t.messages
                        .iter()
                        .any(|m| m.segments.is_empty() || m.segments.len() > 32)
                    || t.id.is_empty()
                    || !ids.insert(&t.id)
                    || t.category.trim().is_empty()
                    || t.description.trim().is_empty()
            })
        {
            bail!("invalid quick replies")
        }
        Ok(replies)
    }
    pub fn instantiate(&self, id: &str, source: &str) -> Result<Envelope> {
        let t = self
            .templates
            .iter()
            .find(|t| t.id == id && t.enabled)
            .ok_or_else(|| anyhow::anyhow!("template unavailable"))?;
        Ok(Envelope {
            to_self: String::new(),
            messages: t
                .messages
                .iter()
                .map(|m| OutgoingMessage {
                    target_ref: Some(source.to_owned()),
                    segments: m.segments.clone(),
                })
                .collect(),
        })
    }
}
