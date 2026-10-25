use crate::{
    ledger::Ledger,
    protocol::{Action, Directory, validate_message},
    review::{Decision, ReviewContext, Reviewer, Verdict},
    sender::Sender,
    tools::ToolRegistry,
};
use anyhow::Result;
use serde_json::{Value, json};
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

pub struct Execution {
    pub feedback: Value,
    pub event_id: Option<i64>,
}

pub struct ActionContext<'a> {
    pub session: &'a str,
    pub turn: &'a str,
    pub source: &'a str,
    pub actor: &'a str,
    pub request: &'a str,
}

pub struct Executor {
    pub ledger: Ledger,
    pub reviewer: Arc<dyn Reviewer>,
    pub sender: Arc<dyn Sender>,
    pub directory: Directory,
    pub tools: ToolRegistry,
    pub cancel: CancellationToken,
}

impl Executor {
    pub async fn execute(
        &self,
        id: &str,
        context: &ActionContext<'_>,
        action: Action,
    ) -> Result<Execution> {
        let directory = if let Some(media) = &self.tools.media {
            let scope = match &action {
                Action::Send { message } => message.target_ref.as_deref().unwrap_or(context.source),
                Action::Tool { arguments, .. } => {
                    arguments["target_ref"].as_str().unwrap_or(context.source)
                }
            };
            media.directory(&self.directory, scope).await?
        } else {
            self.directory.clone()
        };
        let validation = match &action {
            Action::Send { message } => {
                let structural = validate_message(message, &directory);
                if structural.is_ok() {
                    if let Some(media) = &self.tools.media {
                        media.validate_send(message).await
                    } else {
                        structural
                    }
                } else {
                    structural
                }
            }
            Action::Tool { name, arguments } => self.tools.validate(name, arguments),
        };
        if let Err(e) = validation {
            let event_id = self
                .ledger
                .event(
                    context.session,
                    context.turn,
                    "action_rejected",
                    json!({"action_id":id,"action":action,"reason":e.to_string()}),
                )
                .await?;
            return Ok(Execution {
                feedback: json!({"status":"rejected","reason":e.to_string()}),
                event_id: Some(event_id),
            });
        }
        let (revision, facts) = self.ledger.snapshot(context.session).await?;
        let evidence = ReviewContext {
            revision,
            source_target: context.source,
            actor: context.actor,
            user_request: context.request,
            directory: &directory,
            tool_schema: match &action {
                Action::Tool { name, .. } => self
                    .tools
                    .schemas()
                    .into_iter()
                    .find(|s| s["function"]["name"] == *name),
                Action::Send { .. } => None,
            },
            observed_facts: facts,
            proposed_action: &action,
        };
        self.ledger
            .event(
                context.session,
                context.turn,
                "review_started",
                json!({"action_id":id,"action":action}),
            )
            .await?;
        let reviewed = tokio::select! {
            _=self.cancel.cancelled()=>return Ok(Execution {feedback:json!({"status":"cancelled"}),event_id:None}),
            verdict=self.reviewer.review(&evidence)=>verdict,
        };
        let verdict = match reviewed {
            Ok(v) => v,
            Err(_) => Verdict {
                decision: Decision::Reject,
                reason: "independent review unavailable or invalid".into(),
            },
        };
        self.ledger
            .review(
                id,
                revision,
                &action.fingerprint(),
                serde_json::to_value(&verdict)?,
            )
            .await?;
        if matches!(verdict.decision, Decision::Reject) {
            return Ok(Execution {
                feedback: json!({"status":"rejected","reason":verdict.reason}),
                event_id: None,
            });
        }
        if self.cancel.is_cancelled() {
            return Ok(Execution {
                feedback: json!({"status":"cancelled"}),
                event_id: None,
            });
        }
        if let Err(e) = self
            .ledger
            .begin(id, context.session, context.turn, &action, revision)
            .await
        {
            return Ok(Execution {
                feedback: json!({"status":"rejected","reason":e.to_string()}),
                event_id: None,
            });
        }
        // Once started, await the adapter and commit its result even if input is cancelled.
        self.ledger
            .event(
                context.session,
                context.turn,
                "action_started",
                json!({"action_id":id,"action":action}),
            )
            .await?;
        let result = match &action {
            Action::Send { message } => self
                .sender
                .send(id, message)
                .await
                .and_then(|r| Ok(serde_json::to_value(r)?)),
            Action::Tool { name, arguments } => {
                self.tools
                    .execute_with_cancel(name, arguments, context.source, &self.cancel)
                    .await
            }
        };
        let (status, result) = match result {
            Ok(result) => ("succeeded", result),
            Err(error)
                if error
                    .downcast_ref::<crate::workspace::UnknownEffect>()
                    .is_some() =>
            {
                (
                    "unknown",
                    error
                        .downcast_ref::<crate::workspace::UnknownEffect>()
                        .unwrap()
                        .0
                        .clone(),
                )
            }
            Err(error) if error.downcast_ref::<crate::napcat::NotStarted>().is_some() => {
                ("rejected", json!({"reason":error.to_string()}))
            }
            // A sender error may follow a partial stdout write; it is not proof of no effect.
            Err(_) if matches!(action, Action::Send { .. }) => (
                "unknown",
                json!({"reason":"transport did not return a verified receipt; do not replay"}),
            ),
            Err(error) => ("failed", json!({"reason":error.to_string()})),
        };
        let event_id = self.ledger.finish(id, status, result.clone()).await?;
        Ok(Execution {
            feedback: json!({"action_id":id,"status":status,"result":result}),
            event_id: Some(event_id),
        })
    }
}
