//! OneBot arrays at the transport boundary. This module never opens a QQ connection.
use crate::{
    media::{MediaRecord, MediaService},
    protocol::{OutgoingMessage, Segment},
    sender::{Receipt, Sender},
};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc};
use tokio::io::AsyncWriteExt;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum InboundSegment {
    Text {
        text: String,
    },
    Face {
        face_id: u32,
    },
    Image {
        media_id: Option<String>,
        description: String,
        source_ref: String,
    },
}

pub fn normalize_inbound(
    segments: &[Value],
    resolved: &BTreeMap<String, MediaRecord>,
) -> Result<Vec<InboundSegment>> {
    if segments.len() > 64 {
        bail!("inbound segment budget exceeded")
    }
    segments
        .iter()
        .map(|segment| {
            Ok(match segment["type"].as_str() {
                Some("text") => InboundSegment::Text {
                    text: segment["data"]["text"]
                        .as_str()
                        .context("invalid text segment")?
                        .into(),
                },
                Some("face") => {
                    let face_id = segment["data"]["id"]
                        .as_str()
                        .map(str::parse::<u32>)
                        .transpose()?
                        .or_else(|| {
                            segment["data"]["id"]
                                .as_u64()
                                .and_then(|v| v.try_into().ok())
                        })
                        .context("invalid face id")?;
                    InboundSegment::Face { face_id }
                }
                Some("image") => {
                    let source = segment["data"]["file"]
                        .as_str()
                        .or_else(|| segment["data"]["url"].as_str())
                        .context("missing image reference")?;
                    match resolved.get(source) {
                        Some(record) => InboundSegment::Image {
                            media_id: Some(record.id.clone()),
                            description: record.description.clone(),
                            source_ref: source.into(),
                        },
                        None => InboundSegment::Image {
                            media_id: None,
                            description: "图片描述不可用；此位置仍有一张图片，不得猜测内容".into(),
                            source_ref: source.into(),
                        },
                    }
                }
                _ => bail!("unsupported inbound segment type"),
            })
        })
        .collect()
}

pub fn inbound_text(segments: &[InboundSegment]) -> String {
    segments
        .iter()
        .map(|s| match s {
            InboundSegment::Text { text } => text.clone(),
            InboundSegment::Face { face_id } => format!("[QQ face_id={face_id}]"),
            InboundSegment::Image {
                media_id,
                description,
                ..
            } => format!(
                "[图片 media_id={}：{description}]",
                media_id.as_deref().unwrap_or("unavailable")
            ),
        })
        .collect::<Vec<_>>()
        .join("")
}

/// Keep original transport references in the ledger, outside model-facing projections.
pub fn public_inbound(mut data: Value) -> Value {
    if let Some(segments) = data.get_mut("segments").and_then(Value::as_array_mut) {
        for segment in segments {
            if segment["type"] == "image"
                && let Some(fields) = segment.as_object_mut()
            {
                fields.remove("source_ref");
            }
        }
    }
    data
}

pub async fn outbound_array(message: &OutgoingMessage, media: &MediaService) -> Result<Vec<Value>> {
    let target = message
        .target_ref
        .as_deref()
        .context("target_ref required")?;
    let mut output = Vec::new();
    for segment in &message.segments {
        output.push(match segment {
            Segment::Text { text } => json!({"type":"text","data":{"text":text}}),
            Segment::Face { face_id } => json!({"type":"face","data":{"id":face_id.to_string()}}),
            Segment::Image { media_id } => {
                let (record, _) = media.bytes_for_target(media_id, target).await?;
                let path = record
                    .path
                    .to_str()
                    .context("non-UTF8 media path")?
                    .replace('\\', "/");
                let url = if path.starts_with('/') {
                    format!("file://{path}")
                } else {
                    format!("file:///{path}")
                };
                json!({"type":"image","data":{"file":url}})
            }
        })
    }
    Ok(output)
}

/// A durable local capture of the exact outbound OneBot array, not a QQ send.
pub struct OneBotOutbox {
    pub media: Arc<MediaService>,
    pub root: PathBuf,
}

#[async_trait]
impl Sender for OneBotOutbox {
    async fn send(&self, id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        let segments = outbound_array(message, &self.media).await?;
        tokio::fs::create_dir_all(&self.root).await?;
        let file_name = format!("{:x}.json", Sha256::digest(id.as_bytes()));
        let payload = json!({"transport":"local_onebot_outbox","action_id":id,"target_ref":message.target_ref,"message":segments});
        let mut file = tokio::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(self.root.join(file_name))
            .await?;
        file.write_all(serde_json::to_string(&payload)?.as_bytes())
            .await?;
        file.sync_all().await?;
        Ok(Receipt {
            transport: "local_onebot_outbox".into(),
            message_id: format!("outbox:{id}"),
            message: message.clone(),
        })
    }
}
