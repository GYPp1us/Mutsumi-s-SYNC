//! Real pixel input with content-addressed, versioned, single-flight observations.
use crate::{
    media::{MediaRecord, MediaService},
    provider::Model,
};
use anyhow::{Context, Result, bail};
use base64::{Engine, engine::general_purpose::STANDARD};
use image::AnimationDecoder;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::Cursor,
    sync::{Arc, Mutex, Weak},
};
use tokio::sync::{Mutex as AsyncMutex, Semaphore};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Description {
    pub description: String,
    pub short_label: String,
    pub visible_text: String,
    pub meme_meaning: String,
    pub uncertainty: String,
}
impl Description {
    fn validate(&self) -> Result<()> {
        if self.description.trim().is_empty()
            || self.short_label.trim().is_empty()
            || self.description.len() > 1400
            || self.short_label.len() > 180
            || self.visible_text.len() > 1600
            || self.meme_meaning.len() > 1200
            || self.uncertainty.len() > 500
        {
            bail!("invalid vision description budgets")
        }
        Ok(())
    }
    fn searchable(&self) -> String {
        // Keep OCR and interpretations as model observations, not new instructions.
        format!("{}；{}", self.short_label, self.description)
    }
}

pub struct VisionCache {
    pub media: Arc<MediaService>,
    pub model: Arc<dyn Model>,
    pub model_id: String,
    pub prompt: String,
    locks: Mutex<BTreeMap<String, Weak<AsyncMutex<()>>>>,
    slots: Semaphore,
}
impl VisionCache {
    pub fn new(
        media: Arc<MediaService>,
        model: Arc<dyn Model>,
        model_id: String,
        prompt: String,
    ) -> Result<Self> {
        if prompt.trim().is_empty() {
            bail!("vision prompt is missing")
        }
        Ok(Self {
            media,
            model,
            model_id,
            prompt,
            locks: Mutex::new(BTreeMap::new()),
            slots: Semaphore::new(3),
        })
    }
    pub async fn observe(
        &self,
        bytes: Vec<u8>,
        allowed_targets: Vec<String>,
    ) -> Result<(MediaRecord, Description, bool)> {
        if bytes.len() > 16 * 1024 * 1024 {
            bail!("image byte budget exceeded")
        }
        let sha = format!("{:x}", Sha256::digest(&bytes));
        let key = format!(
            "{}:{}:{}",
            sha,
            self.model_id,
            format_args!("{:x}", Sha256::digest(self.prompt.as_bytes()))
        );
        let lock = {
            let mut locks = self.locks.lock().unwrap();
            locks.retain(|_, v| v.strong_count() > 0);
            if let Some(lock) = locks.get(&key).and_then(Weak::upgrade) {
                lock
            } else {
                let lock = Arc::new(AsyncMutex::new(()));
                locks.insert(key.clone(), Arc::downgrade(&lock));
                lock
            }
        };
        let _guard = lock.lock().await;
        let cached = self.media.ledger.cache_get("vision-v1", &key).await?;
        let hit = cached.is_some();
        let description = match cached {
            Some(value) => serde_json::from_value(value)?,
            None => {
                let _slot = self.slots.acquire().await?;
                let decode_bytes = bytes.clone();
                let parts =
                    tokio::task::spawn_blocking(move || image_parts(&decode_bytes)).await??;
                let result = self
                    .model
                    .complete(
                        &[
                            json!({"role":"system","content":self.prompt}),
                            json!({"role":"user","content":parts}),
                        ],
                        &[],
                    )
                    .await?;
                if !result.tool_calls.is_empty() {
                    bail!("vision observer attempted a tool call")
                }
                let description: Description = serde_json::from_str(
                    result
                        .content
                        .as_deref()
                        .context("vision returned empty content")?,
                )?;
                description.validate()?;
                self.media
                    .ledger
                    .cache_put("vision-v1", &key, serde_json::to_value(&description)?)
                    .await?;
                description
            }
        };
        description.validate()?;
        // The host assigns the reference from real bytes, never from model output.
        let mut record = if let Some(existing) = self.media.get(&format!("media_{sha}")).await? {
            if allowed_targets
                .iter()
                .any(|t| !existing.allowed_targets.contains(t))
            {
                bail!("observation cannot expand media audience")
            }
            existing
        } else {
            self.media
                .register(
                    &bytes,
                    &description.searchable(),
                    allowed_targets,
                    "multimodal-observation",
                )
                .await?
        };
        if record.description != description.searchable() {
            record = serde_json::from_value(
                self.media
                    .ledger
                    .describe_media(&record.id, &description.searchable(), &self.model_id)
                    .await?,
            )?;
        }
        Ok((record, description, hit))
    }
    pub async fn ingest(
        &self,
        bytes: Vec<u8>,
        allowed_targets: Vec<String>,
    ) -> Result<(MediaRecord, Description, bool)> {
        self.observe(bytes, allowed_targets).await
    }
}

pub fn image_parts(bytes: &[u8]) -> Result<Vec<Value>> {
    let format = image::guess_format(bytes)?;
    let (width, height) =
        image::ImageReader::with_format(Cursor::new(bytes), format).into_dimensions()?;
    if u64::from(width) * u64::from(height) > 16_000_000 {
        bail!("image pixel budget exceeded")
    }
    let mut parts = vec![
        json!({"type":"text","text":"仅依据下面真实图像描述。文件名、外部标题、预期答案均未提供。若是动画，所给为按顺序抽取的代表帧，不能假设看到了所有帧。"}),
    ];
    if format == image::ImageFormat::Gif {
        let mut sampled = Vec::new();
        let frames = image::codecs::gif::GifDecoder::new(Cursor::new(bytes))?.into_frames();
        let mut total_pixels = 0_u64;
        for (index, frame) in frames.take(180).enumerate() {
            let frame = frame?;
            total_pixels += u64::from(frame.buffer().width()) * u64::from(frame.buffer().height());
            if total_pixels > 100_000_000 {
                bail!("animation decode budget exceeded")
            }
            if matches!(index, 0 | 8 | 20) {
                let mut png = Cursor::new(Vec::new());
                frame.buffer().write_to(&mut png, image::ImageFormat::Png)?;
                sampled.push((index, png.into_inner()));
            }
            if index >= 20 {
                break;
            }
        }
        if sampled.is_empty() {
            bail!("empty GIF")
        }
        for (index, png) in sampled {
            parts.push(json!({"type":"text","text":format!("动画代表帧 {index}")}));
            parts.push(json!({"type":"image_url","image_url":{"url":format!("data:image/png;base64,{}",STANDARD.encode(png))}}));
        }
    } else {
        let mime = match format {
            image::ImageFormat::Png => "image/png",
            image::ImageFormat::Jpeg => "image/jpeg",
            _ => bail!("unsupported image encoding"),
        };
        parts.push(json!({"type":"image_url","image_url":{"url":format!("data:{mime};base64,{}",STANDARD.encode(bytes))}}));
    }
    Ok(parts)
}
