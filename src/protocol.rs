use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub to_self: String,
    pub messages: Vec<OutgoingMessage>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OutgoingMessage {
    // Missing targets reach validation; they never acquire an implicit recipient.
    pub target_ref: Option<String>,
    pub segments: Vec<Segment>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Segment {
    Text { text: String },
    Face { face_id: u32 },
    Image { media_id: String },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub label: String,
    pub account: String,
    pub audience: String,
    pub transport: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Directory {
    pub targets: BTreeMap<String, Target>,
    pub media: BTreeMap<String, String>,
    pub face_ids: Vec<u32>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ToolFunction {
    pub name: String,
    pub arguments: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ToolCall {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub function: ToolFunction,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AssistantRound {
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub tool_calls: Vec<ToolCall>,
    // Kept only in the current provider loop, never persisted or sent.
    #[serde(default)]
    pub reasoning_content: Option<String>,
}

impl AssistantRound {
    pub fn envelope(&self) -> Result<Envelope> {
        let text = self.content.as_deref().unwrap_or("").trim();
        if text.is_empty() && !self.tool_calls.is_empty() {
            return Ok(Envelope::default());
        }
        if text.len() > 64 * 1024 {
            bail!("content exceeds 64 KiB");
        }
        let envelope: Envelope = serde_json::from_str(text)?;
        if envelope.messages.len() > 16 || envelope.to_self.len() > 2048 {
            bail!("too many messages or oversized to_self");
        }
        Ok(envelope)
    }

    pub fn native(&self, content: Option<String>, include_reasoning: bool) -> Value {
        let mut v = serde_json::json!({"role":"assistant", "content":content});
        if !self.tool_calls.is_empty() {
            v["tool_calls"] = serde_json::to_value(&self.tool_calls).unwrap();
        }
        if include_reasoning && let Some(text) = &self.reasoning_content {
            v["reasoning_content"] = Value::String(text.clone());
        }
        v
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Action {
    Send { message: OutgoingMessage },
    Tool { name: String, arguments: Value },
}

impl Action {
    pub fn fingerprint(&self) -> String {
        format!("{:x}", Sha256::digest(serde_json::to_vec(self).unwrap()))
    }
}

pub fn validate_message(message: &OutgoingMessage, directory: &Directory) -> Result<()> {
    let Some(target_ref) = &message.target_ref else {
        bail!("target_ref is required")
    };
    let Some(target) = directory.targets.get(target_ref) else {
        bail!("unknown target_ref")
    };
    if !matches!(
        target.transport.as_str(),
        "terminal" | "onebot_simulator" | "webui" | "napcat"
    ) {
        bail!("transport is not enabled in this prototype")
    }
    if message.segments.is_empty() || message.segments.len() > 32 {
        bail!("invalid segment count")
    }
    for segment in &message.segments {
        match segment {
            Segment::Text { text } if text.trim().is_empty() || text.len() > 16_384 => {
                bail!("invalid text length")
            }
            Segment::Face { face_id } if !directory.face_ids.contains(face_id) => {
                bail!("unknown face_id")
            }
            Segment::Image { media_id } if !directory.media.contains_key(media_id) => {
                bail!("unknown media_id")
            }
            _ => (),
        }
    }
    Ok(())
}
