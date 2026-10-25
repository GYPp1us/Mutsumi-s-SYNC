use crate::{
    ledger::Ledger,
    protocol::{Directory, OutgoingMessage, Segment},
    rendering::RendererWorker,
};
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    io::Cursor,
    path::{Path, PathBuf},
    sync::{Arc, RwLock},
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const MAX_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct MediaRecord {
    pub id: String,
    pub sha256: String,
    pub mime_type: String,
    pub description: String,
    pub width: u32,
    pub height: u32,
    pub path: PathBuf,
    pub allowed_targets: Vec<String>,
    pub origin: String,
}

impl MediaRecord {
    pub fn reference(&self) -> Value {
        json!({"media_id":self.id,"description":self.description,"mime_type":self.mime_type,"width":self.width,"height":self.height})
    }
}

pub struct MediaService {
    pub ledger: Ledger,
    pub worker: Arc<RendererWorker>,
    root: PathBuf,
    targets: RwLock<BTreeSet<String>>,
}

impl MediaService {
    pub async fn new(
        ledger: Ledger,
        root: &Path,
        worker: Arc<RendererWorker>,
        directory: &Directory,
    ) -> Result<Self> {
        tokio::fs::create_dir_all(root).await?;
        Ok(Self {
            ledger,
            worker,
            root: tokio::fs::canonicalize(root).await?,
            targets: RwLock::new(directory.targets.keys().cloned().collect()),
        })
    }
    pub fn known_target(&self, target: &str) -> bool {
        self.targets.read().unwrap().contains(target)
    }
    pub fn add_targets(&self, targets: impl IntoIterator<Item = String>) {
        self.targets.write().unwrap().extend(targets);
    }
    pub async fn register(
        &self,
        bytes: &[u8],
        description: &str,
        allowed_targets: Vec<String>,
        origin: &str,
    ) -> Result<MediaRecord> {
        if bytes.len() > MAX_BYTES
            || description.trim().is_empty()
            || description.len() > 2048
            || allowed_targets.is_empty()
            || allowed_targets.iter().any(|t| !self.known_target(t))
        {
            bail!("invalid media registration")
        }
        let format = image::guess_format(bytes).context("unsupported image bytes")?;
        let (extension, mime) = match format {
            image::ImageFormat::Png => ("png", "image/png"),
            image::ImageFormat::Gif => ("gif", "image/gif"),
            image::ImageFormat::Jpeg => ("jpg", "image/jpeg"),
            _ => bail!("only PNG, JPEG and GIF are supported"),
        };
        let (width, height) =
            image::ImageReader::with_format(Cursor::new(bytes), format).into_dimensions()?;
        if width == 0 || height == 0 || u64::from(width) * u64::from(height) > 16_000_000 {
            bail!("image pixel budget exceeded")
        }
        let sha256 = format!("{:x}", Sha256::digest(bytes));
        let id = format!("media_{sha256}");
        if let Some(existing) = self.get(&id).await? {
            if allowed_targets
                .iter()
                .any(|t| !existing.allowed_targets.contains(t))
            {
                bail!("registration cannot expand an existing media grant")
            }
            return Ok(existing);
        }
        let path = self.root.join(format!("{sha256}.{extension}"));
        match tokio::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)
            .await
        {
            Ok(mut file) => {
                file.write_all(bytes).await?;
                file.sync_all().await?
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if tokio::fs::read(&path).await? != bytes {
                    bail!("media object hash collision or corruption")
                }
            }
            Err(error) => return Err(error.into()),
        }
        let record = MediaRecord {
            id,
            sha256,
            mime_type: mime.into(),
            description: description.into(),
            width,
            height,
            path,
            allowed_targets,
            origin: origin.into(),
        };
        self.ledger
            .put_media(serde_json::to_value(&record)?)
            .await?;
        Ok(record)
    }
    pub async fn get(&self, id: &str) -> Result<Option<MediaRecord>> {
        self.ledger
            .get_media(id)
            .await?
            .map(serde_json::from_value)
            .transpose()
            .map_err(Into::into)
    }
    pub async fn search(&self, query: &str, source_target: &str) -> Result<Value> {
        let words: Vec<_> = query.split_whitespace().map(str::to_lowercase).collect();
        let mut items = Vec::new();
        for value in self.ledger.list_media().await? {
            let record: MediaRecord = serde_json::from_value(value)?;
            if record.allowed_targets.iter().any(|t| t == source_target)
                && (words.is_empty()
                    || words
                        .iter()
                        .any(|w| record.description.to_lowercase().contains(w)))
            {
                items.push(record.reference())
            }
        }
        Ok(json!({"items":items.into_iter().take(32).collect::<Vec<_>>(),"scope":source_target}))
    }
    pub async fn directory(&self, base: &Directory, source: &str) -> Result<Directory> {
        let mut directory = base.clone();
        for value in self.ledger.list_media().await? {
            let record: MediaRecord = serde_json::from_value(value)?;
            if record.allowed_targets.iter().any(|t| t == source) {
                directory.media.insert(record.id, record.description);
            }
        }
        Ok(directory)
    }
    pub async fn bytes_for_target(&self, id: &str, target: &str) -> Result<(MediaRecord, Vec<u8>)> {
        let record = self.get(id).await?.context("unknown media_id")?;
        if !record.allowed_targets.iter().any(|t| t == target) {
            bail!("media is not granted to this audience")
        }
        let canonical = tokio::fs::canonicalize(&record.path).await?;
        if !canonical.starts_with(&self.root) {
            bail!("media path escaped object store")
        }
        let mut bytes = Vec::new();
        tokio::fs::File::open(canonical)
            .await?
            .take((MAX_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .await?;
        if bytes.len() > MAX_BYTES || format!("{:x}", Sha256::digest(&bytes)) != record.sha256 {
            bail!("media bytes do not match the registered hash")
        }
        Ok((record, bytes))
    }
    pub async fn validate_send(&self, message: &OutgoingMessage) -> Result<()> {
        let target = message
            .target_ref
            .as_deref()
            .context("target_ref is required")?;
        for segment in &message.segments {
            if let Segment::Image { media_id } = segment {
                self.bytes_for_target(media_id, target).await?;
            }
        }
        Ok(())
    }
    pub async fn render(
        &self,
        markdown: &str,
        width: u32,
        source: &str,
        target: &str,
    ) -> Result<Value> {
        if !self.known_target(target) {
            bail!("unknown render audience")
        }
        let artifact = self.worker.render(markdown, width).await?;
        let bytes = tokio::fs::read(&artifact.png).await?;
        let targets = BTreeSet::from([source.to_owned(), target.to_owned()])
            .into_iter()
            .filter(|t| self.known_target(t))
            .collect();
        let record = self
            .register(
                &bytes,
                "由本次 Markdown 工具实际生成的图片",
                targets,
                "markdown-render",
            )
            .await?;
        Ok(
            json!({"media":record.reference(),"svg_sha256":format!("{:x}",Sha256::digest(tokio::fs::read(artifact.svg).await?)),"renderer":"md2svg+resvg","worker_pid":artifact.worker_pid}),
        )
    }
}
