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
    let system = if runtime.prompts.persona.trim().is_empty() {
        runtime.prompts.runtime.clone()
    } else {
        format!("{}\n\n{}", runtime.prompts.runtime, runtime.prompts.persona)
    };
    let knowledge = match &runtime.executor.tools.knowledge {
        Some(k) => k.index(&input.session).await?,
        None => Value::Null,
    };
    let (window, omitted) = recent_window(live, &attention, input)?;
    let mut messages = vec![
        json!({"role":"system","content":system}),
        json!({"role":"user","content":json!({"platform_state":{"directory":runtime.config.directory,"media_catalog":catalog,"report_events":match &runtime.executor.tools.events {Some(events)=>events.catalog(false).await?,None=>vec![]},"knowledge":knowledge,"history_window":{"omitted_messages":omitted,"complete_history_retained":true,"retrieval_tool":"history_search"},"profile":profile,"attention":attention,"trigger_source":input.source_target,"trigger_actor":input.actor,"time":chrono::Utc::now().to_rfc3339()}}).to_string()}),
    ];
    messages.extend_from_slice(window);
    if serde_json::to_vec(&messages)?.len() > 512 * 1024 {
        bail!("complete provider context exceeds budget; no silent truncation")
    }
    Ok(messages)
}

/// Bound the historical projection, preserving all unread inputs and complete native tool groups.
fn recent_window<'a>(
    live: &'a [Value],
    attention: &Value,
    input: &Input,
) -> Result<(&'a [Value], usize)> {
    let mut mandatory = live.len();
    if let Some(pending) = attention["pending_events"].as_array() {
        for event in pending {
            let id = event["event_id"].as_str();
            let found = live.iter().position(|m| {
                m["role"] == "user"
                    && m["content"]
                        .as_str()
                        .and_then(|s| serde_json::from_str::<Value>(s).ok())
                        .is_some_and(|v| id.is_some() && v["event_id"].as_str() == id)
            });
            mandatory = mandatory.min(found.ok_or_else(|| {
                anyhow::anyhow!("pending event is not present in the supplied history")
            })?);
        }
    } else if let Some(index) = live.iter().rposition(|m| {
        m["role"] == "user"
            && m["content"]
                .as_str()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .is_some_and(|v| v["actor"] == input.actor && v["text"] == input.text)
    }) {
        mandatory = index;
    }
    let sizes = live
        .iter()
        .map(serde_json::to_vec)
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut remaining = sizes.iter().map(|v| v.len() + 1).sum::<usize>();
    let mut calls = std::collections::BTreeSet::<String>::new();
    let mut cut = 0;
    for (index, message) in live.iter().enumerate() {
        if index <= mandatory && calls.is_empty() && message["role"] != "tool" {
            cut = index;
            if remaining <= 96 * 1024 {
                break;
            }
        }
        if index >= mandatory {
            break;
        }
        if let Some(items) = message["tool_calls"].as_array() {
            for item in items {
                if let Some(id) = item["id"].as_str() {
                    calls.insert(id.into());
                }
            }
        }
        if message["role"] == "tool"
            && let Some(id) = message["tool_call_id"].as_str()
        {
            calls.remove(id);
        }
        remaining = remaining.saturating_sub(sizes[index].len() + 1);
    }
    Ok((&live[cut..], cut))
}
