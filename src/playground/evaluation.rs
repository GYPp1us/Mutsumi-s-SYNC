//! Per-session evidence assessment; expectations never enter the acting runtime.
use crate::{
    config::Config,
    provider::{Model, OpenAiCompatible},
    vision::image_parts,
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};

pub async fn evaluate(
    config: &Config,
    prompt: &str,
    runs: &[Value],
    assets: &[Value],
    root: &Path,
) -> Result<(Value, Vec<Value>)> {
    let mut provider = config.provider.clone();
    // This provider counts reasoning against max_tokens even when thinking=disabled.
    // Assess one session at a time, and retain truncation as an error, never a pass.
    provider.max_tokens = 16384;
    let evaluator = OpenAiCompatible::for_purpose(provider, "evaluation")?;
    let rubric: Value = serde_json::from_slice(&std::fs::read("benchmarks/v2/rubric.json")?)?;
    let mut sessions = Vec::new();
    for run in runs {
        println!("{}: independent multimodal evaluation", run["session"]);
        let events: Vec<Value> = run["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| {
                matches!(
                    e["kind"].as_str(),
                    Some(
                        "inbound"
                            | "media_observation"
                            | "action_result"
                            | "review_started"
                            | "action_rejected"
                            | "turn_end"
                    )
                )
            })
            .map(|e| {
                let mut event = e.clone();
                if event["kind"] == "inbound" {
                    event["data"] = crate::onebot::public_inbound(event["data"].clone());
                }
                event
            })
            .collect();
        let evidence = json!({"session":run["session"],"events":events,"receiver":run["receiver"],"triggers":run["triggers"],"checks":run["checks"]});
        let mut parts =
            vec![json!({"type":"text","text":json!({"rubric":rubric,"run":evidence}).to_string()})];
        for asset in assets {
            parts.push(json!({"type":"text","text":format!("评估用原始素材：{}。素材名及rubric未提供给行动模型的视觉观察器。",asset["id"])}));
            parts.extend(image_parts(&std::fs::read(
                root.join(asset["cache_file"].as_str().unwrap()),
            )?)?);
        }
        let assessed = async {
            let round = evaluator
                .complete(
                    &[
                        json!({"role":"system","content":prompt}),
                        json!({"role":"user","content":parts}),
                    ],
                    &[],
                )
                .await?;
            let value: Value = serde_json::from_str(round.content.as_deref().unwrap_or(""))?;
            validate(&value, &rubric)?;
            Ok::<_, anyhow::Error>(value)
        }
        .await;
        let mut value = assessed
            .unwrap_or_else(|e| json!({"status":"evaluation_error","reason":e.to_string()}));
        value["session"] = run["session"].clone();
        sessions.push(value);
    }
    Ok((json!({"sessions":sessions}), evaluator.metrics()))
}

fn validate(value: &Value, rubric: &Value) -> Result<()> {
    let expected: BTreeSet<&str> = rubric
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|r| r["id"].as_str())
        .collect();
    let Some(checks) = value["checks"].as_array() else {
        bail!("evaluation has no check array")
    };
    let ids: BTreeSet<&str> = checks.iter().filter_map(|c| c["id"].as_str()).collect();
    if ids != expected
        || checks.len() != expected.len()
        || checks.iter().any(|c| {
            !matches!(c["result"].as_str(), Some("pass" | "fail" | "unverified"))
                || c["reason"].as_str().is_none_or(|s| s.is_empty())
                || c["evidence_ids"].as_array().is_none()
        })
    {
        bail!("evaluation is incomplete or has invalid verdicts")
    }
    Ok(())
}
