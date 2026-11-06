//! No platform or provider calls: exercise the same HTTP, identity and persistence boundaries.
use anyhow::Result;
use axum::{Json, routing::get};
use mutsumi_runtime::{
    napcat::NapcatConfig,
    protocol::{OutgoingMessage, Segment},
    qqbot::{Binding, ChannelSender, Config},
    sender::{Sender, TerminalSender},
    web::{ServerConfig, WebState},
};
use serde_json::{Value, json};
use std::sync::{Arc, atomic::Ordering};
use tokio_util::sync::CancellationToken;

#[tokio::test]
async fn official_channel_coexists_with_napcat_and_knowledge_apis_preserve_boundaries() -> Result<()>
{
    let temp = tempfile::tempdir()?;
    let env = if cfg!(windows) { "TEMP" } else { "PATH" };
    let key = std::env::var(env)?;
    let bridge_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let bridge_url = format!("http://{}", bridge_listener.local_addr()?);
    let bridge = tokio::spawn(
        axum::serve(
            bridge_listener,
            axum::Router::new()
                .route(
                    "/status",
                    get(|| async { Json(json!({"ready":true,"app_id":"12345"})) }),
                )
                .route(
                    "/send",
                    axum::routing::post(|Json(body): Json<Value>| async move {
                        assert_eq!(body["reply_id"], "official-1");
                        assert_eq!(body["target_id"], "owner-openid");
                        Json(json!({"status":"succeeded","message_id":"verified-platform-id"}))
                    }),
                ),
        )
        .into_future(),
    );
    let runtime_path = temp.path().join("runtime.yaml");
    let mut config: Value = serde_yaml::from_str(&std::fs::read_to_string(
        "config/server-runtime.example.yaml",
    )?)?;
    config["provider"]["api_key_env"] = json!(env);
    config["database"] = json!(temp.path().join("events.db"));
    config["media"]["root"] = json!(temp.path().join("media"));
    config["workspace"]["root"] = json!(temp.path().join("workspace"));
    config["skills_dir"] = json!(temp.path().join("skills"));
    std::fs::write(&runtime_path, serde_yaml::to_string(&config)?)?;
    let mut settings: ServerConfig =
        serde_yaml::from_str(&std::fs::read_to_string("config/server.example.yaml")?)?;
    settings.runtime_config = runtime_path.display().to_string();
    settings.access_key_env = env.into();
    settings.onebot_hook_key_env = env.into();
    settings.napcat = Some(NapcatConfig {
        base_url: "http://127.0.0.1:9".into(),
        webui_url: "http://127.0.0.1:9".into(),
        token_env: env.into(),
        webui_token_env: env.into(),
        bot_id: "10101".into(),
        owner_id: "20202".into(),
        targets: Default::default(),
    });
    let binding = Binding {
        scope: "c2c".into(),
        conversation_id: "owner-openid".into(),
        label: "官方主人".into(),
        senders: [("owner-openid".into(), "owner".into())].into(),
        allow_proactive: false,
    };
    let target = binding.target("12345");
    settings.qq_official = Some(Config {
        base_url: bridge_url,
        token_env: env.into(),
        app_id: "12345".into(),
        bindings: vec![binding],
    });
    let state = WebState::build(settings).await?;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let stop = CancellationToken::new();
    let server = tokio::spawn(
        axum::serve(listener, mutsumi_runtime::web::router(state.clone()))
            .with_graceful_shutdown(stop.clone().cancelled_owned())
            .into_future(),
    );
    let result: Result<()> = async {
        let http = reqwest::Client::new();
        assert!(!state.qq_enabled.load(Ordering::Acquire));
        assert!(!state.official.as_ref().unwrap().enabled.load(Ordering::Acquire));
        assert_eq!(http.get(format!("{url}/api/knowledge")).send().await?.status(),401);
        let login = http.post(format!("{url}/api/login")).json(&json!({"access_key":key})).send().await?;
        let cookie = login.headers()["set-cookie"].to_str()?.split(';').next().unwrap().to_owned();
        let packet = json!({"app_id":"12345","id":"official-1","scope":"c2c","conversation_id":"owner-openid","actor_id":"owner-openid","occurred_at":chrono::Utc::now().to_rfc3339(),"segments":[{"type":"text","text":"记住工作偏好"},{"type":"image","position":0}]});
        assert_eq!(http.post(format!("{url}/qqbot/events")).json(&packet).send().await?.status(),401);
        let disabled: Value = http.post(format!("{url}/qqbot/events")).bearer_auth(&key).json(&packet).send().await?.json().await?;
        assert_eq!(disabled["ignored"],"disabled");
        assert!(http.post(format!("{url}/api/qqbot/enabled")).header("cookie",&cookie).header("x-sync-ui","1").json(&json!({"enabled":true})).send().await?.status().is_success());
        assert!(!state.qq_enabled.load(Ordering::Acquire));
        let mut forged = packet.clone();forged["relationship"] = json!("owner");
        assert_eq!(http.post(format!("{url}/qqbot/events")).bearer_auth(&key).json(&forged).send().await?.status(),422);
        let mut stranger = packet.clone();stranger["actor_id"] = json!("unregistered");
        let rejected: Value = http.post(format!("{url}/qqbot/events")).bearer_auth(&key).json(&stranger).send().await?.json().await?;
        assert_eq!(rejected["ignored"],"unregistered_identity");
        let mut seq = 0;
        for duplicate in [false,true] {
            let accepted: Value = http.post(format!("{url}/qqbot/events")).bearer_auth(&key).json(&packet).send().await?.json().await?;
            assert_eq!(accepted["duplicate"],duplicate);seq=accepted["seq"].as_i64().unwrap();
        }
        let pending = state.ingress.ledger.pending("live").await?;
        assert_eq!(pending.len(),1);
        assert_eq!(pending[0]["data"]["relationship"],"owner");
        assert_eq!(pending[0]["data"]["actor"],"qqbot:12345:c2c:owner-openid");
        let delivery = state.deliveries.claim("vision").await?.unwrap();
        assert_eq!(delivery.payload["source"],"official");
        let sender = ChannelSender {fallback:Arc::new(TerminalSender(state.runtime.config.directory.clone())),official:state.official.clone().unwrap(),media:state.media.clone(),ledger:state.ingress.ledger.clone()};
        let receipt = sender.send("send-test",&OutgoingMessage {target_ref:Some(target.clone()),segments:vec![Segment::Text{text:"已核对".into()}]}).await?;
        assert_eq!(receipt.transport,"qq_official");assert_eq!(receipt.message_id,"verified-platform-id");
        let status: Value = http.get(format!("{url}/api/status")).header("cookie",&cookie).send().await?.json().await?;
        assert_eq!(status["targets"][&target]["transport"],"qq_official");
        assert!(status["targets"].as_object().unwrap().values().any(|t|t["transport"]=="napcat"));
        let note = json!({"id":"work","expected_version":0,"title":"工作偏好","body":"偏好仍需原始记录支持","evidence_seqs":[seq],"archived":false});
        assert_eq!(http.post(format!("{url}/api/notes")).header("cookie",&cookie).json(&note).send().await?.status(),403);
        for expected in [200,409] {
            assert_eq!(http.post(format!("{url}/api/notes")).header("cookie",&cookie).header("x-sync-ui","1").json(&note).send().await?.status(),expected);
        }
        let skill = json!({"path":"SKILL.md","content":"---\nname: example\ndescription: 测试方法\n---\n\n先检查真实资料。","expected_revision":null});
        let saved: Value = http.post(format!("{url}/api/skills/example")).header("cookie",&cookie).header("x-sync-ui","1").json(&skill).send().await?.json().await?;
        let index: Value = http.get(format!("{url}/api/knowledge")).header("cookie",&cookie).send().await?.json().await?;
        assert_eq!(index["notes"][0]["version"],1);
        assert_eq!(index["skill_catalog"]["skills"][0]["revision"],saved["revision"]);
        let resource: Value = http.get(format!("{url}/api/skills/example")).header("cookie",&cookie).query(&[("path","SKILL.md"),("revision",saved["revision"].as_str().unwrap())]).send().await?.json().await?;
        assert!(resource["content"].as_str().unwrap().contains("先检查真实资料"));
        let history: Value = http.get(format!("{url}/api/history")).header("cookie",&cookie).query(&[("query","工作偏好")]).send().await?.json().await?;
        assert_eq!(history["events"][0]["seq"],seq);
        assert!(history["events"][0]["data"].get("transport").is_none());
        assert!(!index.to_string().contains(&key));
        Ok(())
    }.await;
    stop.cancel();
    server.await??;
    bridge.abort();
    let _ = bridge.await;
    state.shutdown().await?;
    result
}
