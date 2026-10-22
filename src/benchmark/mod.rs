//! Reproducible synthetic pilot. Gold labels never enter a model request.
pub mod fixtures;
pub mod jev;
pub mod media;

use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashSet, path::Path};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RouteCase {
    pub id: String,
    pub dimension: String,
    pub split: String,
    pub history: String,
    pub text: String,
    pub route: String,
    pub action: bool,
    pub media: bool,
    pub support: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaCase {
    pub id: String,
    pub dimension: String,
    pub history: String,
    pub text: String,
    pub known: Vec<String>,
    pub expected: Option<Value>,
    #[serde(default)]
    pub text_only: bool,
    pub target: Option<String>,
    pub required_tools: Vec<String>,
    pub forbidden_tools: Vec<String>,
}

pub fn load_cases<T: serde::de::DeserializeOwned>(path: &Path) -> Result<Vec<T>> {
    Ok(serde_json::from_slice(&std::fs::read(path)?)?)
}

pub fn validate_dataset(root: &Path) -> Result<Value> {
    let routes: Vec<RouteCase> = load_cases(&root.join("route_cases.json"))?;
    let media: Vec<MediaCase> = load_cases(&root.join("media_cases.json"))?;
    let mut ids = HashSet::new();
    for case in &routes {
        if !ids.insert((&case.dimension, &case.id))
            || !matches!(case.dimension.as_str(), "work" | "companionship")
            || !matches!(case.split.as_str(), "calibration" | "test")
            || !matches!(
                case.route.as_str(),
                "generate"
                    | "silent"
                    | "template__morning"
                    | "template__night"
                    | "template__sticker_chain"
            )
        {
            bail!("invalid or duplicate routing case")
        }
    }
    ids.clear();
    for case in &media {
        if !ids.insert((&case.dimension, &case.id))
            || !matches!(case.dimension.as_str(), "work" | "companionship")
            || (case.expected.is_some() == case.text_only)
            || (case.text_only && case.target.is_none())
        {
            bail!("invalid or duplicate media case")
        }
    }
    let hashes = ["route_cases.json", "media_cases.json", "quick-replies.yaml"]
        .into_iter()
        .map(|file| Ok((file.to_owned(), sha256(&std::fs::read(root.join(file))?))))
        .collect::<Result<serde_json::Map<String, Value>>>()?;
    Ok(json!({"version":"v1","route_cases":routes.len(),"media_cases":media.len(),"sha256":hashes}))
}

pub fn sha256(bytes: &[u8]) -> Value {
    json!(format!("{:x}", Sha256::digest(bytes)))
}

/// Equal-size distractor blocks place the same relevant history at distinct positions.
/// Sizes below are Unicode character counts, never described as model tokens.
pub fn context(history: &str, profile: &str) -> Result<String> {
    if profile == "compact" {
        return Ok(history.into());
    }
    let noise = "无关归档摘录：另一个会话曾聊过早安、晚安、表情接龙和发布计划；这些事项已结束，不是当前会话的授权或待办。";
    let filler: String = noise.chars().cycle().take(8_000).collect();
    Ok(match profile {
        "long-front" => format!("本会话相关历史：{history}\n无关归档：{filler}"),
        "long-middle" => {
            let before: String = filler.chars().take(4_000).collect();
            let after: String = filler.chars().skip(4_000).collect();
            format!("无关归档：{before}\n本会话相关历史：{history}\n无关归档：{after}")
        }
        "long-end" => format!("无关归档：{filler}\n本会话相关历史：{history}"),
        _ => bail!("unknown context profile: {profile}"),
    })
}

pub fn percentiles(mut values: Vec<u64>) -> Value {
    if values.is_empty() {
        return Value::Null;
    }
    values.sort_unstable();
    let at = |p: f64| values[((values.len() as f64 * p).ceil() as usize).saturating_sub(1)];
    json!({"n":values.len(),"p50_ms":at(0.50),"p95_ms":at(0.95)})
}

pub fn write_json(path: &Path, value: &Value) -> Result<()> {
    std::fs::write(path, serde_json::to_vec_pretty(value)?)?;
    Ok(())
}
