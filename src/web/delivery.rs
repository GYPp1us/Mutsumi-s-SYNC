//! Internal media delivery resumes from SQLite after process interruption.
use super::WebState;
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};

impl WebState {
    pub async fn deliveries_work(self: Arc<Self>) -> Result<()> {
        loop {
            if self.cancel.is_cancelled() {
                break;
            }
            let Some(claim) = self.deliveries.claim("vision").await? else {
                tokio::select! {_=self.cancel.cancelled()=>break,_=tokio::time::sleep(Duration::from_millis(500))=>()};
                continue;
            };
            let result = tokio::select! {_=self.cancel.cancelled()=>break,r=self.observe_delivery(&claim.payload)=>r};
            let observations = match result {
                Ok(observations) => observations,
                Err(_) if claim.attempts < 2 => {
                    self.deliveries
                        .retry(
                            claim,
                            "media observation unavailable; retrying internal delivery",
                        )
                        .await?;
                    continue;
                }
                Err(_) => {
                    let count = claim.payload["media_ids"]
                        .as_array()
                        .or_else(|| claim.payload["images"].as_array())
                        .map(Vec::len)
                        .unwrap_or(0);
                    json!((0..count).map(|position|json!({"position":position,"status":"unavailable","reason":"媒体处理多次失败，原始入站仍保留；不要猜测内容"})).collect::<Vec<_>>())
                }
            };
            let id = claim.payload["event_id"]
                .as_str()
                .context("delivery event id missing")?
                .to_owned();
            let payload = json!({"event_id":id,"observations":observations,"observed_at":chrono::Utc::now().to_rfc3339()});
            self.deliveries
                .complete(
                    claim,
                    Some(("live".into(), id, "media_observation".into(), payload)),
                )
                .await?;
            self.ingress.changed.notify_one();
        }
        Ok(())
    }
    async fn observe_delivery(&self, payload: &Value) -> Result<Value> {
        let target = payload["target_ref"]
            .as_str()
            .context("delivery target missing")?;
        let mut observations = Vec::new();
        if payload["source"] == "web" {
            for (position, id) in payload["media_ids"]
                .as_array()
                .context("delivery media missing")?
                .iter()
                .enumerate()
            {
                let (record, bytes) = self
                    .media
                    .bytes_for_target(id.as_str().context("invalid media id")?, target)
                    .await?;
                let (record, description, hit) = self
                    .models
                    .current()
                    .vision
                    .ingest(bytes, record.allowed_targets)
                    .await?;
                observations.push(json!({"position":position,"status":"available","data":{"media":record.reference(),"visual_observation":description,"vision_cache_hit":hit}}));
            }
        } else {
            for (position, image) in payload["images"]
                .as_array()
                .context("delivery images missing")?
                .iter()
                .enumerate()
            {
                let (record, bytes) = if payload["source"] == "napcat" {
                    let client = self
                        .napcat
                        .as_ref()
                        .context("NapCat removed while image is pending")?;
                    let file = image["file"].as_str().unwrap_or("");
                    let reference = if file.is_empty() {
                        image["url"].as_str().context("image reference missing")?
                    } else {
                        file
                    };
                    let key = format!("{}:{reference}", client.config.bot_id);
                    let cached = self.ingress.ledger.cache_get("qq-image", &key).await?;
                    let bytes = if let Some(id) = cached.and_then(|v| v.as_str().map(str::to_owned))
                    {
                        self.media.bytes_for_target(&id, target).await?.1
                    } else {
                        client.image_bytes(file, image["url"].as_str()).await?
                    };
                    let record = self
                        .media
                        .register(
                            &bytes,
                            "图像尚待观察",
                            vec![target.into(), "web_owner".into()],
                            "qq-private-ingress",
                        )
                        .await?;
                    self.ingress
                        .ledger
                        .cache_put("qq-image", &key, json!(record.id))
                        .await?;
                    (record, bytes)
                } else {
                    let client = self
                        .official
                        .as_ref()
                        .context("official QQ adapter is not configured")?;
                    let bytes = client
                        .image(
                            payload["event_key"].as_str().context("event key missing")?,
                            position,
                        )
                        .await?;
                    let record = self
                        .media
                        .register(
                            &bytes,
                            "图像尚待观察",
                            vec![target.into(), "web_owner".into()],
                            "qq-official-ingress",
                        )
                        .await?;
                    (record, bytes)
                };
                let (record, description, hit) = self
                    .models
                    .current()
                    .vision
                    .ingest(bytes, record.allowed_targets)
                    .await?;
                observations.push(json!({"position":position,"status":"available","data":{"media":record.reference(),"visual_observation":description,"vision_cache_hit":hit}}));
            }
        }
        Ok(json!(observations))
    }
}
