use super::{MediaCase, context, fixtures, load_cases, percentiles, write_json};
use crate::{
    config::Config,
    executor::Executor,
    ledger::Ledger,
    media::{MediaRecord, MediaService},
    onebot::{OneBotOutbox, inbound_text, outbound_array},
    protocol::{AssistantRound, OutgoingMessage},
    provider::{Model, OpenAiCompatible},
    rendering::RendererWorker,
    review::ModelReviewer,
    routing::{MainModelRouter, QuickReplies},
    runtime::{Input, Runtime},
    tools::ToolRegistry,
};
use anyhow::{Context, Result};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::Write,
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

struct MeasuredModel {
    inner: Arc<dyn Model>,
    calls: Mutex<Vec<Value>>,
}

impl MeasuredModel {
    fn new(inner: Arc<dyn Model>) -> Self {
        Self {
            inner,
            calls: Mutex::new(Vec::new()),
        }
    }
    fn trace(&self) -> Vec<Value> {
        self.calls.lock().unwrap().clone()
    }
}

#[async_trait]
impl Model for MeasuredModel {
    async fn complete(&self, messages: &[Value], tools: &[Value]) -> Result<AssistantRound> {
        let start = Instant::now();
        let response = self.inner.complete(messages, tools).await;
        let mut record = json!({"elapsed_ms":start.elapsed().as_millis() as u64,"request_bytes":serde_json::to_vec(messages)?.len()});
        match &response {
            Ok(round) => {
                record["assistant"] = round.native(round.content.clone(), false);
            }
            Err(error) => {
                record["error"] = json!(error.to_string());
            }
        }
        self.calls.lock().unwrap().push(record);
        response
    }
}

fn replace_ids(
    value: &mut Value,
    records: &BTreeMap<String, MediaRecord>,
    rendered: Option<&str>,
) -> Result<()> {
    match value {
        Value::String(s) if s.starts_with('$') => {
            let alias = &s[1..];
            *s = if alias == "rendered" {
                rendered.context("no successful render result")?.into()
            } else {
                records
                    .get(alias)
                    .context("unknown fixture alias")?
                    .id
                    .clone()
            };
        }
        Value::Array(items) => {
            for item in items {
                replace_ids(item, records, rendered)?;
            }
        }
        Value::Object(items) => {
            for value in items.values_mut() {
                replace_ids(value, records, rendered)?;
            }
        }
        _ => (),
    }
    Ok(())
}

/// Exact segment and message boundaries; text-only cases measure protocol, not empathy.
pub fn matches_messages(case: &MediaCase, actual: &[Value], expected: Option<&Value>) -> bool {
    if case.text_only {
        return actual.len() == 1
            && actual[0]["target_ref"] == case.target.as_deref().unwrap_or("")
            && actual[0]["segments"].as_array().is_some_and(|segments| {
                !segments.is_empty()
                    && segments.iter().all(|s| {
                        s["type"] == "text"
                            && s["text"].as_str().is_some_and(|s| !s.trim().is_empty())
                    })
            });
    }
    expected.is_some_and(|expected| expected == &json!(actual))
}

pub async fn run(
    dataset: &Path,
    output: &Path,
    profiles: &[String],
    limit: Option<usize>,
    worker_executable: &Path,
) -> Result<Value> {
    let worker = Arc::new(
        RendererWorker::new(
            worker_executable,
            &output.join("render-jobs"),
            Duration::from_secs(60),
        )
        .await?,
    );
    let result = run_cases(dataset, output, profiles, limit, worker.clone()).await;
    let shutdown = worker.shutdown().await;
    let report = result?;
    shutdown?;
    Ok(report)
}

async fn run_cases(
    dataset: &Path,
    output: &Path,
    profiles: &[String],
    limit: Option<usize>,
    worker: Arc<RendererWorker>,
) -> Result<Value> {
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let provider: Arc<dyn Model> = Arc::new(OpenAiCompatible::new(config.provider.clone())?);
    let cases: Vec<MediaCase> = load_cases(&dataset.join("media_cases.json"))?;
    let mut rows = Vec::new();
    let mut results = std::fs::File::create(output.join("results.jsonl"))?;
    for case in cases.iter().take(limit.unwrap_or(usize::MAX)) {
        for profile in profiles {
            let id = format!("{}-{}-{profile}", case.dimension, case.id);
            let case_root = output.join(&id);
            tokio::fs::create_dir_all(&case_root).await?;
            let ledger = Ledger::open(&case_root.join("events.sqlite3")).await?;
            let service = Arc::new(
                MediaService::new(
                    ledger.clone(),
                    &case_root.join("objects"),
                    worker.clone(),
                    &config.directory,
                )
                .await?,
            );
            let records = fixtures::register(&service).await?;
            let mut case_config = config.clone();
            for alias in &case.known {
                if let Some(record) = records.get(alias) {
                    case_config
                        .directory
                        .media
                        .insert(record.id.clone(), record.description.clone());
                }
            }
            let prompts = case_config.prompts()?;
            let main = Arc::new(MeasuredModel::new(provider.clone()));
            let reviewer = Arc::new(MeasuredModel::new(provider.clone()));
            let runtime = Runtime {
                config: case_config.clone(),
                prompts: prompts.clone(),
                model: main.clone(),
                executor: Executor {
                    ledger: ledger.clone(),
                    reviewer: Arc::new(ModelReviewer {
                        model: reviewer.clone(),
                        system: prompts.reviewer,
                    }),
                    sender: Arc::new(OneBotOutbox {
                        media: service.clone(),
                        root: case_root.join("outbox"),
                    }),
                    directory: case_config.directory,
                    tools: ToolRegistry {
                        media: Some(service.clone()),
                    },
                    cancel: CancellationToken::new(),
                },
                router: Arc::new(MainModelRouter),
                quick_replies: QuickReplies::load(&dataset.join("quick-replies.yaml"))?,
            };
            let history = context(&case.history, profile)?;
            ledger.event(&id, "fixture-history", "inbound", json!({"actor":"synthetic_owner","source_target":"owner_private","text":history,"origin":"synthetic_benchmark_history"})).await?;
            let segments = fixtures::inbound(&case.text, &case.known, &records)?;
            let text = inbound_text(&segments);
            let started = Instant::now();
            let outcome = runtime
                .run(Input {
                    session: id.clone(),
                    source_target: "owner_private".into(),
                    actor: "synthetic_owner".into(),
                    text,
                    segments,
                })
                .await;
            let mut row = json!({"id":case.id,"dimension":case.dimension,"profile":profile,"elapsed_ms":started.elapsed().as_millis() as u64,"history_chars":history.chars().count(),"history_bytes":history.len()});
            let main_trace = main.trace();
            let review_trace = reviewer.trace();
            match outcome {
                Ok(outcome) => {
                    let events = ledger.turn_events(&id, &outcome.turn_id).await?;
                    let actions: Vec<_> = events
                        .iter()
                        .filter(|e| e["kind"] == "action_result")
                        .map(|e| &e["data"])
                        .collect();
                    let actual: Vec<_> = actions
                        .iter()
                        .filter(|e| e["status"] == "succeeded" && e["action"]["kind"] == "send")
                        .map(|e| e["result"]["message"].clone())
                        .collect();
                    let tools: Vec<_> = actions
                        .iter()
                        .filter(|e| e["status"] == "succeeded" && e["action"]["kind"] == "tool")
                        .filter_map(|e| e["action"]["name"].as_str())
                        .collect();
                    let rendered = actions
                        .iter()
                        .find(|e| {
                            e["status"] == "succeeded" && e["action"]["name"] == "render_markdown"
                        })
                        .and_then(|e| e["result"]["media"]["media_id"].as_str());
                    let mut expected = case.expected.clone();
                    let expected_resolved = expected
                        .as_mut()
                        .is_none_or(|v| replace_ids(v, &records, rendered).is_ok());
                    let mut first_batch = None;
                    let mut proposed_tools = Vec::new();
                    let mut malformed = 0;
                    for call in &main_trace {
                        if let Some(raw) = call.get("assistant") {
                            let round: AssistantRound = serde_json::from_value(raw.clone())?;
                            for tool in &round.tool_calls {
                                proposed_tools.push(tool.function.name.clone());
                            }
                            match round.envelope() {
                                Ok(envelope)
                                    if !envelope.messages.is_empty() && first_batch.is_none() =>
                                {
                                    first_batch = Some(
                                        envelope
                                            .messages
                                            .iter()
                                            .map(|m| serde_json::to_value(m).unwrap())
                                            .collect::<Vec<_>>(),
                                    );
                                }
                                Err(_) => malformed += 1,
                                _ => (),
                            }
                        }
                    }
                    let forbidden_proposals: Vec<_> = proposed_tools
                        .iter()
                        .filter(|t| case.forbidden_tools.contains(t))
                        .collect();
                    let tools_ok = case
                        .required_tools
                        .iter()
                        .all(|t| tools.contains(&t.as_str()))
                        && forbidden_proposals.is_empty();
                    let output_ok =
                        expected_resolved && matches_messages(case, &actual, expected.as_ref());
                    let first_ok = expected_resolved
                        && first_batch
                            .as_ref()
                            .is_some_and(|batch| matches_messages(case, batch, expected.as_ref()))
                        && forbidden_proposals.is_empty()
                        && malformed == 0;
                    let mut transport = Vec::new();
                    for message in &actual {
                        let message: OutgoingMessage = serde_json::from_value(message.clone())?;
                        transport.push(json!({"target_ref":message.target_ref,"segments":outbound_array(&message, &service).await?}));
                    }
                    row["status"] = json!(outcome.status);
                    row["first_output_conformance"] = json!(first_ok);
                    row["end_to_end_pass"] =
                        json!(outcome.status == "completed" && output_ok && tools_ok);
                    row["output_conformance"] = json!(output_ok);
                    row["tools_conformance"] = json!(tools_ok);
                    row["rejected_actions"] = json!(outcome.rejected_count);
                    row["malformed_rounds"] = json!(malformed);
                    row["forbidden_tool_proposals"] = json!(forbidden_proposals);
                    row["actual_messages"] = json!(actual);
                    row["successful_tools"] = json!(tools);
                    row["transport"] = json!(transport);
                    write_json(
                        &case_root.join("trace.json"),
                        &json!({"case_id":id,"main":main_trace,"reviewer":review_trace,"events":events,"outcome":outcome}),
                    )?;
                }
                Err(error) => {
                    row["status"] = json!("runtime_error");
                    row["error"] = json!(error.to_string());
                }
            }
            row["main_calls"] = json!(main_trace.len());
            row["review_calls"] = json!(review_trace.len());
            row["main_latency"] = percentiles(
                main_trace
                    .iter()
                    .filter_map(|r| r["elapsed_ms"].as_u64())
                    .collect(),
            );
            row["review_latency"] = percentiles(
                review_trace
                    .iter()
                    .filter_map(|r| r["elapsed_ms"].as_u64())
                    .collect(),
            );
            writeln!(results, "{row}")?;
            results.flush()?;
            println!("{id}: {} pass={}", row["status"], row["end_to_end_pass"]);
            rows.push(row);
        }
    }
    let mut groups = Vec::new();
    for dimension in ["work", "companionship"] {
        for profile in profiles {
            let selected: Vec<_> = rows
                .iter()
                .filter(|r| r["dimension"] == dimension && r["profile"] == *profile)
                .collect();
            groups.push(json!({"dimension":dimension,"profile":profile,"attempted":selected.len(),"end_to_end_pass":selected.iter().filter(|r|r["end_to_end_pass"]==true).count(),"first_output_conformance":selected.iter().filter(|r|r["first_output_conformance"]==true).count(),"rejected_actions":selected.iter().filter_map(|r|r["rejected_actions"].as_u64()).sum::<u64>(),"main_calls":selected.iter().filter_map(|r|r["main_calls"].as_u64()).sum::<u64>(),"review_calls":selected.iter().filter_map(|r|r["review_calls"].as_u64()).sum::<u64>(),"latency":percentiles(selected.iter().filter_map(|r|r["elapsed_ms"].as_u64()).collect())}));
        }
    }
    let report = json!({"kind":"media-instruction-following","model":config.provider.model,"review_model":config.provider.model,"transport":"local_onebot_outbox","groups":groups,"limitations":["no QQ connection or delivery verification","synthetic captions are supplied; no vision accuracy claim","text-only checks measure protocol, not emotional quality","single sample per case/profile; variants are correlated","token usage unavailable in this adapter"]});
    write_json(&output.join("summary.json"), &report)?;
    Ok(report)
}
