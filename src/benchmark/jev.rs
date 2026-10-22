use super::{RouteCase, context, load_cases, percentiles, write_json};
use crate::{
    config::Config,
    jev::{JevClient, JevConfig, JevPrompts, routing_state},
    routing::{QuickReplies, RouteContext},
};
use anyhow::Result;
use serde_json::{Value, json};
use std::{io::Write, path::Path, time::Instant};

pub async fn run(
    dataset: &Path,
    output: &Path,
    profiles: &[String],
    limit: Option<usize>,
    model: Option<String>,
) -> Result<Value> {
    let mut settings: JevConfig =
        serde_yaml::from_str(&std::fs::read_to_string("config/jev.example.yaml")?)?;
    if let Some(model) = model {
        settings.model = model;
    }
    let prompts = JevPrompts::load(Path::new(&settings.prompts))?;
    let replies = QuickReplies::load(&dataset.join("quick-replies.yaml"))?;
    let criteria = prompts.criteria(&replies);
    let questions = prompts.questions(&replies, true);
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let client = JevClient::new(settings.clone())?;
    let cases: Vec<RouteCase> = load_cases(&dataset.join("route_cases.json"))?;
    let mut file = std::fs::File::create(output.join("results.jsonl"))?;
    let mut rows = Vec::new();
    for case in cases.iter().take(limit.unwrap_or(usize::MAX)) {
        for profile in profiles {
            let history = context(&case.history, profile)?;
            let state = routing_state(&RouteContext {
                text: &case.text,
                actor: "synthetic_owner",
                source_target: "owner_private",
                directory: &config.directory,
                recent_facts: vec![
                    json!({"kind":"inbound","data":{"source_target":"owner_private","text":history}}),
                ],
            });
            let started = Instant::now();
            let mut row = json!({"id":case.id,"dimension":case.dimension,"split":case.split,"profile":profile,"expected_route":case.route,"state_chars":state.to_string().chars().count(),"state_bytes":state.to_string().len()});
            let response = client.evaluate(&state, &questions).await;
            row["elapsed_ms"] = json!(started.elapsed().as_millis() as u64);
            match response.and_then(|r| {
                let choice = r.route_choice(&criteria)?;
                Ok((r, choice))
            }) {
                Ok((response, choice)) => {
                    let policy = if client.accepted(&choice) {
                        choice.choice.as_str()
                    } else {
                        "generate"
                    };
                    row["status"] = json!("ok");
                    row["raw_correct"] = json!(choice.choice == case.route);
                    row["policy_correct"] = json!(policy == case.route);
                    row["policy_route"] = json!(policy);
                    row["unsafe_fast"] = json!(case.route == "generate" && policy != "generate");
                    row["raw_unsafe_fast"] =
                        json!(case.route == "generate" && choice.choice != "generate");
                    row["response"] = serde_json::to_value(response)?;
                    let mut scores = serde_json::Map::new();
                    for (key, gold) in [
                        ("requires_action", case.action),
                        ("requires_media", case.media),
                        ("needs_personalized_support", case.support),
                    ] {
                        if let Some(p) = row["response"]["answers"][key]["noul"]
                            .as_f64()
                            .filter(|p| (0.0..=1.0).contains(p))
                        {
                            scores.insert(key.into(), json!({"gold":gold,"probability":p,"brier":(p-f64::from(gold)).powi(2)}));
                        }
                    }
                    row["diagnostics"] = json!(scores);
                }
                Err(error) => {
                    row["status"] = json!("error");
                    row["error"] = json!(error.to_string());
                }
            }
            writeln!(file, "{row}")?;
            file.flush()?;
            println!(
                "{} {} {} {}",
                case.dimension, case.id, profile, row["status"]
            );
            rows.push(row);
        }
    }
    let mut groups = Vec::new();
    for split in ["calibration", "test"] {
        for dimension in ["work", "companionship"] {
            for profile in profiles {
                let selected: Vec<_> = rows
                    .iter()
                    .filter(|r| {
                        r["split"] == split
                            && r["dimension"] == dimension
                            && r["profile"] == *profile
                    })
                    .collect();
                let valid: Vec<_> = selected
                    .iter()
                    .filter(|r| r["status"] == "ok")
                    .copied()
                    .collect();
                let count = |key: &str| valid.iter().filter(|r| r[key] == true).count();
                groups.push(json!({"split":split,"dimension":dimension,"profile":profile,"attempted":selected.len(),"valid":valid.len(),"errors":selected.len()-valid.len(),"raw_correct":count("raw_correct"),"policy_correct":count("policy_correct"),"raw_unsafe_fast":count("raw_unsafe_fast"),"unsafe_fast":count("unsafe_fast"),"must_generate":valid.iter().filter(|r|r["expected_route"]=="generate").count(),"accepted_templates":valid.iter().filter(|r|r["policy_route"].as_str().is_some_and(|s|s.starts_with("template__"))).count(),"latency":percentiles(valid.iter().filter_map(|r|r["elapsed_ms"].as_u64()).collect())}));
            }
        }
    }
    let report = json!({"kind":"jev-text-routing","requested_model":settings.model,"min_probability":settings.min_probability,"min_confidence":settings.min_confidence,"long_context_header":settings.long_context,"thresholds_predeclared":true,"groups":groups,"limitations":["synthetic pilot gold, not independently human validated","no vision evaluation","variants of one case are correlated","no threshold fitting or option-order robustness claim"]});
    write_json(&output.join("summary.json"), &report)?;
    Ok(report)
}
