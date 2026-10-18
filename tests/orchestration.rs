use anyhow::{Result, bail};
use async_trait::async_trait;
use mutsumi_runtime::{
    config::Config,
    executor::Executor,
    ledger::Ledger,
    protocol::{Action, AssistantRound, Envelope, OutgoingMessage, Segment},
    provider::Model,
    review::{Decision, ModelReviewer, ReviewContext, Reviewer, Verdict},
    routing::{MainModelRouter, QuickReplies, Route, RouteContext, Router},
    runtime::{Input, Runtime},
    sender::{Receipt, Sender},
    tools::ToolRegistry,
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    path::Path,
    sync::{Arc, Mutex},
};
use tempfile::TempDir;
use tokio_util::sync::CancellationToken;

struct ScriptedModel {
    rounds: Mutex<VecDeque<AssistantRound>>,
    requests: Mutex<Vec<Vec<Value>>>,
}
impl ScriptedModel {
    fn new(rounds: Vec<AssistantRound>) -> Self {
        Self {
            rounds: Mutex::new(rounds.into()),
            requests: Mutex::new(vec![]),
        }
    }
}
#[async_trait]
impl Model for ScriptedModel {
    async fn complete(&self, messages: &[Value], _tools: &[Value]) -> Result<AssistantRound> {
        self.requests.lock().unwrap().push(messages.to_vec());
        self.rounds
            .lock()
            .unwrap()
            .pop_front()
            .ok_or_else(|| anyhow::anyhow!("no scripted response"))
    }
}

#[derive(Default)]
struct AllowReview {
    actions: Mutex<Vec<Action>>,
}
#[async_trait]
impl Reviewer for AllowReview {
    async fn review(&self, e: &ReviewContext<'_>) -> Result<Verdict> {
        self.actions.lock().unwrap().push(e.proposed_action.clone());
        Ok(Verdict {
            decision: Decision::Allow,
            reason: "test evidence permits action".into(),
        })
    }
}

#[derive(Default)]
struct CaptureSender {
    sent: Mutex<Vec<OutgoingMessage>>,
    fail_at: Option<usize>,
    cancel_after: Option<CancellationToken>,
}
#[async_trait]
impl Sender for CaptureSender {
    async fn send(&self, id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        let mut sent = self.sent.lock().unwrap();
        if self.fail_at == Some(sent.len()) {
            bail!("simulated transport interruption")
        }
        sent.push(message.clone());
        if let Some(token) = &self.cancel_after {
            token.cancel()
        }
        Ok(Receipt {
            transport: "test".into(),
            message_id: id.into(),
            message: message.clone(),
        })
    }
}

fn message(target: Option<&str>, text: &str) -> OutgoingMessage {
    OutgoingMessage {
        target_ref: target.map(str::to_owned),
        segments: vec![Segment::Text { text: text.into() }],
    }
}
fn round(messages: Vec<OutgoingMessage>, tool: bool) -> AssistantRound {
    AssistantRound {
        content: Some(
            serde_json::to_string(&Envelope {
                to_self: "短记".into(),
                messages,
            })
            .unwrap(),
        ),
        tool_calls: if tool {
            serde_json::from_value(json!([{"id":"call_clock","type":"function","function":{"name":"clock","arguments":"{}"}}])).unwrap()
        } else {
            vec![]
        },
        reasoning_content: Some("private reasoning marker".into()),
    }
}
fn input(text: &str) -> Input {
    Input {
        session: "test-session".into(),
        source_target: "owner_private".into(),
        actor: "test_owner".into(),
        text: text.into(),
    }
}

async fn fixture(
    rounds: Vec<AssistantRound>,
    sender: Arc<dyn Sender>,
) -> (TempDir, Runtime, Arc<ScriptedModel>, Arc<AllowReview>) {
    let dir = TempDir::new().unwrap();
    let config = Config::load(Path::new("config/v4.example.yaml")).unwrap();
    let prompts = config.prompts().unwrap();
    let model = Arc::new(ScriptedModel::new(rounds));
    let reviewer = Arc::new(AllowReview::default());
    let ledger = Ledger::open(&dir.path().join("events.sqlite3"))
        .await
        .unwrap();
    let executor = Executor {
        ledger,
        reviewer: reviewer.clone(),
        sender,
        directory: config.directory.clone(),
        tools: ToolRegistry,
        cancel: CancellationToken::new(),
    };
    let runtime = Runtime {
        quick_replies: QuickReplies::load(Path::new("quick-replies.yaml")).unwrap(),
        config,
        prompts,
        model: model.clone(),
        executor,
        router: Arc::new(MainModelRouter),
    };
    (dir, runtime, model, reviewer)
}

#[tokio::test]
async fn tool_round_content_sends_before_tool_and_preserves_native_pairs() {
    let sender = Arc::new(CaptureSender::default());
    let (_dir, runtime, model, review) = fixture(
        vec![
            round(vec![message(Some("owner_private"), "我先查一下")], true),
            round(vec![message(Some("owner_private"), "已查到时间")], false),
        ],
        sender.clone(),
    )
    .await;
    let outcome = runtime.run(input("查询现在时间")).await.unwrap();
    assert_eq!(
        (
            outcome.status.as_str(),
            outcome.sent_count,
            outcome.tool_count
        ),
        ("completed", 2, 1)
    );
    {
        let actions = review.actions.lock().unwrap();
        assert!(matches!(actions[0], Action::Send { .. }));
        assert!(matches!(actions[1], Action::Tool { .. }));
        let requests = model.requests.lock().unwrap();
        let second = &requests[1];
        let index = second
            .iter()
            .position(|m| m["tool_calls"].is_array())
            .unwrap();
        assert_eq!(second[index + 1]["role"], "tool");
        assert_eq!(second[index + 1]["tool_call_id"], "call_clock");
        assert!(
            second
                .iter()
                .any(|m| m["content"].as_str().unwrap_or("").contains("send_results"))
        );
    }
    let history = runtime
        .executor
        .ledger
        .history("test-session")
        .await
        .unwrap();
    let serialized = serde_json::to_string(&history.messages).unwrap();
    assert!(!serialized.contains("private reasoning marker"));
    assert!(!serialized.contains("短记"));
    let native = history
        .messages
        .iter()
        .find(|m| m["tool_calls"].is_array())
        .unwrap();
    assert!(native["content"].is_null());
}

#[tokio::test]
async fn missing_target_is_rejected_and_never_defaults_to_source() {
    let sender = Arc::new(CaptureSender::default());
    let (_dir, runtime, _, review) = fixture(
        vec![
            round(vec![message(None, "不得发出去")], true),
            round(vec![], false),
        ],
        sender.clone(),
    )
    .await;
    let out = runtime.run(input("查时间")).await.unwrap();
    assert_eq!(out.sent_count, 0);
    assert_eq!(out.rejected_count, 1);
    assert_eq!(out.tool_count, 1);
    assert!(sender.sent.lock().unwrap().is_empty());
    assert_eq!(review.actions.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn malformed_content_executes_no_tools_and_gets_bounded_correction() {
    let sender = Arc::new(CaptureSender::default());
    let mut bad = round(vec![], true);
    bad.content = Some("{broken".into());
    let (_dir, runtime, model, review) = fixture(vec![bad, round(vec![], false)], sender).await;
    let out = runtime.run(input("查询时间")).await.unwrap();
    assert_eq!(out.tool_count, 0);
    assert!(review.actions.lock().unwrap().is_empty());
    assert!(
        model.requests.lock().unwrap()[1]
            .iter()
            .any(|m| m["role"] == "tool")
    );
}

#[tokio::test]
async fn unknown_partial_send_keeps_success_and_never_retries_remainder() {
    let sender = Arc::new(CaptureSender {
        fail_at: Some(1),
        ..Default::default()
    });
    let (_dir, runtime, model, _) = fixture(
        vec![round(
            vec![
                message(Some("owner_private"), "第一条"),
                message(Some("owner_private"), "第二条"),
                message(Some("owner_private"), "第三条"),
            ],
            false,
        )],
        sender.clone(),
    )
    .await;
    let out = runtime.run(input("发送三条")).await.unwrap();
    assert_eq!(out.status, "unknown_receipt");
    assert_eq!(out.sent_count, 1);
    assert_eq!(sender.sent.lock().unwrap().len(), 1);
    assert_eq!(model.requests.lock().unwrap().len(), 1);
    let facts = runtime.executor.ledger.facts("test-session").await.unwrap();
    assert!(facts.iter().any(|x| x["data"]["status"] == "succeeded"));
    assert!(facts.iter().any(|x| x["data"]["status"] == "unknown"));
}

#[tokio::test]
async fn cancellation_after_send_keeps_receipt_and_cancels_tool_with_native_result() {
    let cancel = CancellationToken::new();
    let sender = Arc::new(CaptureSender {
        cancel_after: Some(cancel.clone()),
        ..Default::default()
    });
    let (_dir, mut runtime, _, _) = fixture(
        vec![round(vec![message(Some("owner_private"), "已发")], true)],
        sender,
    )
    .await;
    runtime.executor.cancel = cancel;
    let out = runtime.run(input("发送后查询时间")).await.unwrap();
    assert_eq!(out.status, "cancelled");
    assert_eq!(out.sent_count, 1);
    assert_eq!(out.tool_count, 0);
    let history = runtime
        .executor
        .ledger
        .history("test-session")
        .await
        .unwrap();
    let text = serde_json::to_string(&history.messages).unwrap();
    assert!(text.contains("succeeded"));
    assert!(text.contains("cancelled"));
    assert!(!text.contains("短记"));
}

#[tokio::test]
async fn stale_approval_and_changed_parameters_are_rejected() {
    let dir = TempDir::new().unwrap();
    let ledger = Ledger::open(&dir.path().join("db")).await.unwrap();
    ledger
        .event("s", "t", "inbound", json!({"text":"first"}))
        .await
        .unwrap();
    let revision = ledger.revision().await.unwrap();
    let action = Action::Tool {
        name: "clock".into(),
        arguments: json!({}),
    };
    ledger
        .review(
            "a",
            revision,
            &action.fingerprint(),
            json!({"decision":"allow","reason":"test"}),
        )
        .await
        .unwrap();
    let changed = Action::Tool {
        name: "clock".into(),
        arguments: json!({"changed":true}),
    };
    assert!(
        ledger
            .begin("a", "s", "t", &changed, revision)
            .await
            .is_err()
    );
    ledger
        .event("s", "t2", "inbound", json!({"text":"stop"}))
        .await
        .unwrap();
    assert!(
        ledger
            .begin("a", "s", "t", &action, revision)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn crash_recovery_marks_started_unknown_and_restores_real_native_call() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("db");
    {
        let ledger = Ledger::open(&path).await.unwrap();
        ledger
            .event("s", "t", "inbound", json!({"text":"clock"}))
            .await
            .unwrap();
        ledger
            .round("t:0", "s", "t", &round(vec![], true))
            .await
            .unwrap();
        let action = Action::Tool {
            name: "clock".into(),
            arguments: json!({}),
        };
        let revision = ledger.revision().await.unwrap();
        ledger
            .review(
                "t:0:tool:call_clock",
                revision,
                &action.fingerprint(),
                json!({"decision":"allow","reason":"test"}),
            )
            .await
            .unwrap();
        ledger
            .begin("t:0:tool:call_clock", "s", "t", &action, revision)
            .await
            .unwrap();
    }
    let ledger = Ledger::open(&path).await.unwrap();
    let history = ledger.history("s").await.unwrap();
    assert!(
        history
            .messages
            .iter()
            .any(|m| m["tool_call_id"] == "call_clock"
                && m["content"].as_str().unwrap_or("").contains("unknown"))
    );
    assert!(
        !serde_json::to_string(&history.messages)
            .unwrap()
            .contains("private reasoning marker")
    );
}

#[tokio::test]
async fn reviewer_gets_only_independent_evidence_and_cannot_execute_tools() {
    let model = Arc::new(ScriptedModel::new(vec![AssistantRound {
        content: Some(r#"{"decision":"allow","reason":"supported"}"#.into()),
        tool_calls: vec![],
        reasoning_content: None,
    }]));
    let reviewer = ModelReviewer {
        model: model.clone(),
        system: "独立审查".into(),
    };
    let config = Config::load(Path::new("config/v4.example.yaml")).unwrap();
    let action = Action::Tool {
        name: "clock".into(),
        arguments: json!({}),
    };
    let evidence = ReviewContext {
        revision: 7,
        source_target: "owner_private",
        actor: "owner",
        user_request: "查时间",
        directory: &config.directory,
        tool_schema: ToolRegistry.schemas().into_iter().next(),
        observed_facts: vec![],
        proposed_action: &action,
    };
    assert!(matches!(
        reviewer.review(&evidence).await.unwrap().decision,
        Decision::Allow
    ));
    let requests = model.requests.lock().unwrap();
    assert_eq!(requests[0].len(), 2);
    assert!(!requests[0][1].to_string().contains("reasoning_content"));
}

struct TemplateRouter;
#[async_trait]
impl Router for TemplateRouter {
    async fn route(&self, _: &RouteContext<'_>) -> Result<Route> {
        Ok(Route::Template("morning".into()))
    }
}

#[tokio::test]
async fn prepared_reply_uses_explicit_binding_and_same_review_without_main_model() {
    let sender = Arc::new(CaptureSender::default());
    let (_dir, mut runtime, model, review) = fixture(vec![], sender).await;
    runtime.router = Arc::new(TemplateRouter);
    let out = runtime.run(input("早安")).await.unwrap();
    assert_eq!(out.sent_count, 1);
    assert_eq!(out.model_rounds, 0);
    assert!(model.requests.lock().unwrap().is_empty());
    assert_eq!(review.actions.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn database_rejects_second_owner_and_results_commit_exactly_once() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("db");
    let ledger = Ledger::open(&path).await.unwrap();
    assert!(Ledger::open(&path).await.is_err());
    let action = Action::Tool {
        name: "clock".into(),
        arguments: json!({}),
    };
    ledger
        .review("a", 0, &action.fingerprint(), json!({"decision":"allow"}))
        .await
        .unwrap();
    ledger.begin("a", "s", "t", &action, 0).await.unwrap();
    ledger
        .finish("a", "succeeded", json!({"time":"verified"}))
        .await
        .unwrap();
    assert!(
        ledger
            .finish("a", "succeeded", json!({"time":"duplicate"}))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn new_tool_fact_invalidates_review_just_like_new_inbound() {
    let dir = TempDir::new().unwrap();
    let ledger = Ledger::open(&dir.path().join("db")).await.unwrap();
    let action = Action::Tool {
        name: "clock".into(),
        arguments: json!({}),
    };
    ledger
        .review("a", 0, &action.fingerprint(), json!({"decision":"allow"}))
        .await
        .unwrap();
    ledger
        .event(
            "s",
            "other",
            "action_result",
            json!({"status":"succeeded","result":"new fact"}),
        )
        .await
        .unwrap();
    assert!(ledger.begin("a", "s", "t", &action, 0).await.is_err());
}

#[tokio::test]
async fn message_segments_keep_inline_order_and_separate_message_boundary() {
    let sender = Arc::new(CaptureSender::default());
    let mixed = OutgoingMessage {
        target_ref: Some("owner_private".into()),
        segments: vec![
            Segment::Text {
                text: "文字".into(),
            },
            Segment::Face { face_id: 14 },
            Segment::Image {
                media_id: "registered_media".into(),
            },
            Segment::Text {
                text: "末尾 | 原文".into(),
            },
        ],
    };
    let (_dir, mut runtime, _, _) = fixture(
        vec![round(
            vec![mixed, message(Some("project_group"), "独立一条")],
            false,
        )],
        sender.clone(),
    )
    .await;
    runtime
        .config
        .directory
        .media
        .insert("registered_media".into(), "terminal-preview-only".into());
    runtime.executor.directory = runtime.config.directory.clone();
    let out = runtime.run(input("测试混排和分条")).await.unwrap();
    assert_eq!(out.sent_count, 2);
    let sent = sender.sent.lock().unwrap();
    assert_eq!(sent[0].segments.len(), 4);
    assert!(
        matches!(&sent[0].segments[2],Segment::Image{media_id} if media_id=="registered_media")
    );
    assert_eq!(sent[1].target_ref.as_deref(), Some("project_group"));
}

#[tokio::test]
async fn malformed_output_exhausts_correction_without_unreviewed_fallback_send() {
    let mut bad = round(vec![], false);
    bad.content = Some("not a protocol envelope".into());
    let sender = Arc::new(CaptureSender::default());
    let (_dir, runtime, model, review) =
        fixture(vec![bad.clone(), bad.clone(), bad], sender.clone()).await;
    let out = runtime.run(input("请回答")).await.unwrap();
    assert_eq!(out.status, "protocol_error");
    assert_eq!(model.requests.lock().unwrap().len(), 3);
    assert!(sender.sent.lock().unwrap().is_empty());
    assert!(review.actions.lock().unwrap().is_empty());
}

#[tokio::test]
async fn provider_failure_retains_inbound_and_explicit_failed_turn() {
    let sender = Arc::new(CaptureSender::default());
    let (_dir, runtime, _, _) = fixture(vec![], sender).await;
    let out = runtime.run(input("不能丢掉的入站")).await.unwrap();
    assert_eq!(out.status, "provider_error");
    let history = runtime
        .executor
        .ledger
        .history("test-session")
        .await
        .unwrap();
    assert!(history.messages.iter().any(|m| {
        m["content"]
            .as_str()
            .unwrap_or("")
            .contains("不能丢掉的入站")
    }));
}

#[tokio::test]
async fn foreign_database_is_rejected_without_modifying_its_schema() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("foreign.sqlite3");
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute_batch("CREATE TABLE legacy(value TEXT);")
        .unwrap();
    drop(connection);
    assert!(Ledger::open(&path).await.is_err());
    let connection = rusqlite::Connection::open(&path).unwrap();
    let count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

struct RejectReview;
#[async_trait]
impl Reviewer for RejectReview {
    async fn review(&self, _: &ReviewContext<'_>) -> Result<Verdict> {
        Ok(Verdict {
            decision: Decision::Reject,
            reason: "insufficient target authorization".into(),
        })
    }
}

#[tokio::test]
async fn semantic_review_rejection_never_sends_draft_or_bypasses_gate() {
    let sender = Arc::new(CaptureSender::default());
    let proposed = round(
        vec![message(Some("project_group"), "不该发送的草稿")],
        false,
    );
    let (_dir, mut runtime, _, _) = fixture(
        vec![proposed.clone(), proposed.clone(), proposed],
        sender.clone(),
    )
    .await;
    runtime.executor.reviewer = Arc::new(RejectReview);
    let out = runtime.run(input("只对当前私聊回复")).await.unwrap();
    assert_eq!(out.status, "review_rejected");
    assert_eq!(out.sent_count, 0);
    assert!(sender.sent.lock().unwrap().is_empty());
    let history = runtime
        .executor
        .ledger
        .history("test-session")
        .await
        .unwrap();
    let projection = serde_json::to_string(&history.messages).unwrap();
    assert!(!projection.contains("不该发送的草稿"));
    assert!(projection.contains("insufficient target authorization"));
}
