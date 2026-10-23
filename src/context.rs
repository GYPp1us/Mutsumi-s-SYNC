//! One provider-context assembler, shared by CLI, scheduler, and playground.
use crate::{
    media::MediaRecord,
    runtime::{Input, Runtime},
};
use anyhow::{Result, bail};
use serde_json::{Value, json};

pub async fn request(
    runtime: &Runtime,
    input: &Input,
    live: &[Value],
    through_seq: i64,
) -> Result<Vec<Value>> {
    let mut catalog = Vec::new();
    if runtime.executor.tools.media.is_some() {
        for item in runtime.executor.ledger.list_media().await? {
            let record: MediaRecord = serde_json::from_value(item)?;
            let mut reference = record.reference();
            reference["allowed_targets"] = json!(record.allowed_targets);
            catalog.push(reference);
        }
    }
    let profile = runtime
        .executor
        .ledger
        .cache_get("runtime", "profile")
        .await?;
    let attention = if input.source_target == "attention" {
        runtime
            .executor
            .ledger
            .attention(&input.session, through_seq)
            .await?
    } else {
        Value::Null
    };
    let mut messages = vec![
        json!({"role":"system","content":format!("{}\n\n{}",runtime.prompts.runtime,runtime.prompts.persona)}),
        json!({"role":"user","content":json!({"platform_state":{"directory":runtime.config.directory,"media_catalog":catalog,"profile":profile,"attention":attention,"trigger_source":input.source_target,"trigger_actor":input.actor,"time":chrono::Utc::now().to_rfc3339()}}).to_string()}),
    ];
    messages.extend_from_slice(live);
    if serde_json::to_vec(&messages)?.len() > 512 * 1024 {
        bail!("complete provider context exceeds budget; no silent truncation")
    }
    Ok(messages)
}
