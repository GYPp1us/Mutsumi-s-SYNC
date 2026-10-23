use crate::{
    config::{Config, Prompts},
    executor::{ActionContext, Executor},
    protocol::{Action, Envelope},
    provider::Model,
    routing::{QuickReplies, Route, RouteContext, Router},
};
use anyhow::{Result, bail};
use serde::Serialize;
use serde_json::{Value, json};
use std::sync::Arc;
use uuid::Uuid;

pub struct Input {
    pub session: String,
    pub source_target: String,
    pub actor: String,
    pub text: String,
    pub segments: Vec<crate::onebot::InboundSegment>,
}

#[derive(Debug, Serialize)]
pub struct TurnOutcome {
    pub turn_id: String,
    pub status: String,
    pub model_rounds: usize,
    pub sent_count: usize,
    pub tool_count: usize,
    pub rejected_count: usize,
}

pub struct Runtime {
    pub config: Config,
    pub prompts: Prompts,
    pub model: Arc<dyn Model>,
    pub executor: Executor,
    pub router: Arc<dyn Router>,
    pub quick_replies: QuickReplies,
}

impl Runtime {
    pub async fn run(&self, input: Input) -> Result<TurnOutcome> {
        self.run_inner(input, true).await
    }
    pub async fn run_pending(&self, session: &str) -> Result<TurnOutcome> {
        self.run_inner(Input{session:session.into(),source_target:"attention".into(),actor:"host_scheduler".into(),text:"处理尚未完成的入站事件；逐个核对来源和实际回执，自行决定回复、静默、目标与工具。".into(),segments:vec![]},false).await
    }
    async fn run_inner(&self, input: Input, append: bool) -> Result<TurnOutcome> {
        if input.text.trim().is_empty()
            || input.text.len() > 16_384
            || input.actor.is_empty()
            || input.session.is_empty()
        {
            bail!("invalid inbound input");
        }
        if append
            && !self
                .config
                .directory
                .targets
                .contains_key(&input.source_target)
        {
            bail!("source target is not in the directory")
        }
        let turn = Uuid::new_v4().to_string();
        if append {
            self.executor.ledger.event(&input.session,&turn,"inbound",json!({
            "actor":input.actor,"source_target":input.source_target,"text":input.text,
            "time":chrono::Utc::now().with_timezone(&chrono::FixedOffset::east_opt(28800).unwrap()).to_rfc3339(),
            "origin":"terminal_ingress","segments":input.segments
        })).await?;
        }
        let mut history = self.executor.ledger.history(&input.session).await?;
        self.executor
            .ledger
            .save_history(&input.session, &history, None)
            .await?;
        let mut live = history.messages.clone();
        let mut outcome = TurnOutcome {
            turn_id: turn.clone(),
            status: "round_limit".into(),
            model_rounds: 0,
            sent_count: 0,
            tool_count: 0,
            rejected_count: 0,
        };
        let route_context = RouteContext {
            text: &input.text,
            actor: &input.actor,
            source_target: &input.source_target,
            directory: &self.config.directory,
            recent_facts: self.executor.ledger.facts(&input.session).await?,
        };
        let route_result = tokio::select! {
            _=self.executor.cancel.cancelled()=>Ok(Route::Silent),
            result=self.router.route(&route_context)=>result,
        };
        let route = match route_result {
            Ok(route) => route,
            Err(error) => {
                self.executor
                    .ledger
                    .event(
                        &input.session,
                        &turn,
                        "routing_error",
                        json!({"reason":error.to_string(),"fallback":"generate"}),
                    )
                    .await?;
                Route::Generate
            }
        };
        let template = match route {
            Route::Template(id) => self
                .quick_replies
                .instantiate(&id, &input.source_target)
                .ok(),
            Route::Silent => {
                outcome.status = if self.executor.cancel.is_cancelled() {
                    "cancelled"
                } else {
                    "silent"
                }
                .into();
                self.end(&input, &outcome).await?;
                return Ok(outcome);
            }
            Route::Generate => None,
        };
        let mut notes = Vec::new();
        let mut corrections = 0;
        let action_context = ActionContext {
            session: &input.session,
            turn: &turn,
            source: &input.source_target,
            actor: &input.actor,
            request: &input.text,
        };
        for index in 0..self.config.max_rounds {
            if self.executor.cancel.is_cancelled() {
                outcome.status = "cancelled".into();
                break;
            }
            // New ingress is projected at a provider boundary, preserving the current native pairs.
            let refreshed = self.executor.ledger.history(&input.session).await?;
            if refreshed.messages.len() > history.messages.len() {
                live.extend_from_slice(&refreshed.messages[history.messages.len()..]);
                history = refreshed;
            }
            // Refuse oversized history instead of silently discarding facts or half a tool pair.
            if serde_json::to_vec(&live)?.len() > 256 * 1024 {
                outcome.status = "context_limit".into();
                break;
            }
            let round = if let Some(template) = template.as_ref().filter(|_| index == 0) {
                crate::protocol::AssistantRound {
                    content: Some(serde_json::to_string(template)?),
                    tool_calls: vec![],
                    reasoning_content: None,
                }
            } else {
                let request =
                    match crate::context::request(self, &input, &live, history.through_seq).await {
                        Ok(request) => request,
                        Err(_) => {
                            outcome.status = "context_limit".into();
                            break;
                        }
                    };
                let schemas = self.executor.tools.schemas();
                if self.config.capture_context {
                    self.executor
                        .ledger
                        .event(
                            &input.session,
                            &turn,
                            "context_snapshot",
                            json!({"round_index":index,"messages":request,"tools":schemas}),
                        )
                        .await?;
                }
                let result = tokio::select! {
                    _=self.executor.cancel.cancelled()=>{outcome.status="cancelled".into();break},
                    result=self.model.complete(&request,&schemas)=>result,
                };
                match result {
                    Ok(round) => {
                        outcome.model_rounds += 1;
                        round
                    }
                    Err(error) => {
                        self.executor
                            .ledger
                            .event(
                                &input.session,
                                &turn,
                                "provider_error",
                                json!({"reason":error.to_string()}),
                            )
                            .await?;
                        outcome.status = "provider_error".into();
                        break;
                    }
                }
            };
            let round_id = format!("{turn}:{index}");
            let event = self
                .executor
                .ledger
                .round(&round_id, &input.session, &turn, &round)
                .await?;
            history.event_ids.push(event);
            live.push(round.native(round.content.clone(), true));
            let envelope = match round.envelope() {
                Ok(envelope) => envelope,
                Err(error) => {
                    // Preserve every native call/result pair; malformed content executes nothing.
                    if !round.tool_calls.is_empty() {
                        history.messages.push(round.native(None, false));
                        for call in &round.tool_calls {
                            let result = json!({"role":"tool","tool_call_id":call.id,"content":json!({"status":"rejected","reason":"invalid content JSON; no action executed"}).to_string()});
                            live.push(result.clone());
                            history.messages.push(result);
                        }
                    }
                    let feedback = observation(
                        json!({"content_error":error.to_string(),"nothing_executed":true}),
                    );
                    live.push(feedback.clone());
                    history.messages.push(feedback);
                    self.executor
                        .ledger
                        .save_history(&input.session, &history, Some(&round_id))
                        .await?;
                    corrections += 1;
                    if corrections > self.config.max_corrections {
                        outcome.status = "protocol_error".into();
                        break;
                    }
                    continue;
                }
            };
            let mut send_feedback = Vec::new();
            let mut sent = Vec::new();
            let mut unknown = false;
            let mut rejected = false;
            for (message_index, message) in envelope.messages.iter().enumerate() {
                let result = if unknown {
                    crate::executor::Execution {
                        feedback: json!({"status":"cancelled","reason":"earlier send has unknown receipt"}),
                        event_id: None,
                    }
                } else {
                    self.executor
                        .execute(
                            &format!("{round_id}:send:{message_index}"),
                            &action_context,
                            Action::Send {
                                message: message.clone(),
                            },
                        )
                        .await?
                };
                if let Some(id) = result.event_id {
                    history.event_ids.push(id)
                }
                match result.feedback["status"].as_str() {
                    Some("succeeded") => {
                        outcome.sent_count += 1;
                        sent.push(message.clone())
                    }
                    Some("unknown") => unknown = true,
                    Some("rejected") => {
                        outcome.rejected_count += 1;
                        rejected = true
                    }
                    _ => (),
                }
                send_feedback.push(result.feedback);
            }
            if !round.tool_calls.is_empty() {
                history.messages.push(round.native(None, false))
            } else if !sent.is_empty() {
                history.messages.push(round.native(
                    Some(serde_json::to_string(&Envelope {
                        to_self: String::new(),
                        messages: sent,
                    })?),
                    false,
                ))
            }
            for call in &round.tool_calls {
                let result = match serde_json::from_str::<Value>(&call.function.arguments) {
                    Ok(arguments) => {
                        self.executor
                            .execute(
                                &format!("{round_id}:tool:{}", call.id),
                                &action_context,
                                Action::Tool {
                                    name: call.function.name.clone(),
                                    arguments,
                                },
                            )
                            .await?
                    }
                    Err(_) => crate::executor::Execution {
                        feedback: json!({"status":"rejected","reason":"invalid tool arguments JSON"}),
                        event_id: None,
                    },
                };
                if let Some(id) = result.event_id {
                    history.event_ids.push(id)
                }
                if result.feedback["status"] == "succeeded" {
                    outcome.tool_count += 1
                }
                if result.feedback["status"] == "rejected" {
                    outcome.rejected_count += 1
                }
                let feedback = json!({"role":"tool","tool_call_id":call.id,"content":result.feedback.to_string()});
                live.push(feedback.clone());
                history.messages.push(feedback);
            }
            if !send_feedback.is_empty() {
                let feedback = observation(json!({"send_results":send_feedback}));
                live.push(feedback.clone());
                history.messages.push(feedback);
            }
            if !envelope.to_self.is_empty() {
                notes.push(envelope.to_self)
            }
            self.executor
                .ledger
                .save_history(&input.session, &history, Some(&round_id))
                .await?;
            if self.executor.cancel.is_cancelled() {
                outcome.status = "cancelled".into();
                break;
            }
            if unknown {
                outcome.status = "unknown_receipt".into();
                break;
            }
            if round.tool_calls.is_empty() {
                if rejected {
                    corrections += 1;
                    if corrections > self.config.max_corrections {
                        outcome.status = "review_rejected".into();
                        break;
                    }
                } else {
                    outcome.status = "completed".into();
                    break;
                }
            }
        }
        if outcome.status == "completed" && !notes.is_empty() {
            self.executor
                .ledger
                .event(&input.session, &turn, "inner_note", json!({"notes":notes}))
                .await?;
        }
        self.end(&input, &outcome).await?;
        Ok(outcome)
    }

    async fn end(&self, input: &Input, outcome: &TurnOutcome) -> Result<()> {
        self.executor
            .ledger
            .event(
                &input.session,
                &outcome.turn_id,
                "turn_end",
                serde_json::to_value(outcome)?,
            )
            .await?;
        Ok(())
    }
}

fn observation(data: Value) -> Value {
    json!({"role":"user","content":json!({"platform_observation":data}).to_string()})
}
