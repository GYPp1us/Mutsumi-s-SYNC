use anyhow::Result;
use image::{AnimationDecoder, codecs::gif::GifDecoder};
use mutsumi_runtime::{
    benchmark::{self, fixtures},
    config::Config,
    jev::{JevPrompts, JevResponse, routing_state},
    ledger::Ledger,
    media::MediaService,
    onebot::{InboundSegment, inbound_text, normalize_inbound, outbound_array},
    protocol::{OutgoingMessage, Segment},
    rendering::RendererWorker,
    routing::{QuickReplies, RouteContext},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, io::Cursor, path::Path, sync::Arc, time::Duration};

#[tokio::test]
async fn native_arrays_keep_inline_order_gif_bytes_and_message_boundaries() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let ledger = Ledger::open(&temp.path().join("events.sqlite3")).await?;
    let worker = Arc::new(
        RendererWorker::new(
            Path::new("not-started"),
            &temp.path().join("jobs"),
            Duration::from_secs(60),
        )
        .await?,
    );
    let service = MediaService::new(
        ledger,
        &temp.path().join("objects"),
        worker,
        &config.directory,
    )
    .await?;
    let records = fixtures::register(&service).await?;
    let gif = &records["sleepy_cat"];
    let (_, bytes) = service.bytes_for_target(&gif.id, "owner_private").await?;
    let frames = GifDecoder::new(Cursor::new(bytes.clone()))?
        .into_frames()
        .collect_frames()?;
    assert_eq!(frames.len(), 2);
    assert_eq!(std::fs::read(&gif.path)?, bytes);
    let inline = OutgoingMessage {
        target_ref: Some("owner_private".into()),
        segments: vec![
            Segment::Text {
                text: "之前".into(),
            },
            Segment::Image {
                media_id: gif.id.clone(),
            },
            Segment::Face { face_id: 14 },
            Segment::Text {
                text: "之后".into(),
            },
        ],
    };
    let actual = outbound_array(&inline, &service).await?;
    assert_eq!(
        actual
            .iter()
            .map(|s| s["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["text", "image", "face", "text"]
    );
    assert!(
        actual[1]["data"]["file"]
            .as_str()
            .unwrap()
            .ends_with(".gif")
    );
    let independent = OutgoingMessage {
        target_ref: Some("owner_private".into()),
        segments: vec![Segment::Image {
            media_id: gif.id.clone(),
        }],
    };
    assert_eq!(outbound_array(&independent, &service).await?.len(), 1);
    Ok(())
}

#[tokio::test]
async fn unknown_image_is_retained_between_known_image_and_text() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let ledger = Ledger::open(&temp.path().join("events.sqlite3")).await?;
    let worker = Arc::new(
        RendererWorker::new(
            Path::new("not-started"),
            &temp.path().join("jobs"),
            Duration::from_secs(60),
        )
        .await?,
    );
    let service = MediaService::new(
        ledger,
        &temp.path().join("objects"),
        worker,
        &config.directory,
    )
    .await?;
    let records = fixtures::register(&service).await?;
    let resolved = BTreeMap::from([("first".into(), records["chart_a"].clone())]);
    let segments = normalize_inbound(
        &[
            json!({"type":"image","data":{"file":"first"}}),
            json!({"type":"image","data":{"file":"missing"}}),
            json!({"type":"text","data":{"text":"只看第二张"}}),
        ],
        &resolved,
    )?;
    assert_eq!(segments.len(), 3);
    assert!(matches!(
        &segments[0],
        InboundSegment::Image {
            media_id: Some(_),
            ..
        }
    ));
    assert!(matches!(
        &segments[1],
        InboundSegment::Image { media_id: None, .. }
    ));
    let projection = inbound_text(&segments);
    assert!(
        projection.find(&records["chart_a"].id).unwrap() < projection.find("unavailable").unwrap()
    );
    assert!(projection.find("unavailable").unwrap() < projection.find("只看第二张").unwrap());
    Ok(())
}

#[tokio::test]
async fn audience_hash_and_duplicate_grant_checks_reject_real_files() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let ledger = Ledger::open(&temp.path().join("events.sqlite3")).await?;
    let worker = Arc::new(
        RendererWorker::new(
            Path::new("not-started"),
            &temp.path().join("jobs"),
            Duration::from_secs(60),
        )
        .await?,
    );
    let service = MediaService::new(
        ledger,
        &temp.path().join("objects"),
        worker,
        &config.directory,
    )
    .await?;
    let records = fixtures::register(&service).await?;
    let private = &records["private_note"];
    assert!(
        service
            .bytes_for_target(&private.id, "project_group")
            .await
            .is_err()
    );
    let bytes = std::fs::read(&private.path)?;
    assert!(
        service
            .register(
                &bytes,
                "promote to public",
                vec!["owner_private".into(), "project_group".into()],
                "retry"
            )
            .await
            .is_err()
    );
    assert_eq!(
        service.search("私密", "project_group").await?["items"],
        json!([])
    );
    std::fs::write(&private.path, b"corrupt")?;
    assert!(
        service
            .bytes_for_target(&private.id, "owner_private")
            .await
            .is_err()
    );
    Ok(())
}

#[test]
fn dataset_labels_are_separate_from_jev_model_state_and_choices_are_validated() -> Result<()> {
    let manifest = benchmark::validate_dataset(Path::new("benchmarks/v1"))?;
    assert_eq!(manifest["route_cases"], 24);
    assert_eq!(manifest["media_cases"], 16);
    let config = Config::load(Path::new("config/v4.example.yaml"))?;
    let prompts = JevPrompts::load(Path::new("jev-prompts.yaml"))?;
    let replies = QuickReplies::load(Path::new("benchmarks/v1/quick-replies.yaml"))?;
    let criteria = prompts.criteria(&replies);
    let state = routing_state(&RouteContext {
        text: "晚安，明天提醒我开会",
        actor: "owner",
        source_target: "owner_private",
        directory: &config.directory,
        recent_facts: vec![],
    });
    assert!(state.get("route").is_none() && state.get("expected_route").is_none());
    let mut probabilities: BTreeMap<_, _> = criteria.keys().map(|k| (k.clone(), 0.0)).collect();
    probabilities.insert("generate".into(), 1.0);
    let mut response = JevResponse {
        model: "fixture".into(),
        answers: BTreeMap::from([(
            "route".into(),
            json!({"choice":"generate","confidence":0.99,"probabilities":probabilities}),
        )]),
        usage: Value::Null,
    };
    assert!(response.route_choice(&criteria).is_ok());
    response.answers.get_mut("route").unwrap()["choice"] = json!("unknown_template");
    assert!(response.route_choice(&criteria).is_err());
    response.answers.get_mut("route").unwrap()["choice"] = json!("generate");
    response.answers.get_mut("route").unwrap()["probabilities"]["generate"] = json!(0.4);
    assert!(response.route_choice(&criteria).is_err());
    Ok(())
}

#[tokio::test]
async fn raw_media_source_is_persisted_but_not_shared_with_models() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let ledger = Ledger::open(&temp.path().join("events.sqlite3")).await?;
    ledger.event("session","turn","inbound",json!({"text":"查看图片","segments":[{"type":"image","media_id":"known","description":"fixture","source_ref":"fixture://private-download-token"}]})).await?;
    let raw = ledger.turn_events("session", "turn").await?;
    assert!(serde_json::to_string(&raw)?.contains("private-download-token"));
    let history = ledger.history("session").await?;
    assert!(!serde_json::to_string(&history.messages)?.contains("private-download-token"));
    assert!(
        !serde_json::to_string(&ledger.facts("session").await?)?.contains("private-download-token")
    );
    assert!(
        !serde_json::to_string(&ledger.snapshot("session").await?.1)?
            .contains("private-download-token")
    );
    assert!(serde_json::to_string(&history.messages)?.contains("known"));
    Ok(())
}

#[tokio::test]
async fn version_one_ledger_migrates_without_losing_events() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("events.sqlite3");
    let ledger = Ledger::open(&path).await?;
    ledger
        .event("s", "t", "inbound", json!({"text":"preserve"}))
        .await?;
    drop(ledger);
    let connection = rusqlite::Connection::open(&path)?;
    connection.execute_batch("DROP TABLE media; PRAGMA user_version=1;")?;
    drop(connection);
    let migrated = Ledger::open(&path).await?;
    assert_eq!(migrated.facts("s").await?[0]["data"]["text"], "preserve");
    assert!(migrated.list_media().await?.is_empty());
    Ok(())
}

#[tokio::test]
#[ignore = "requires MUTSUMI_RENDER_WORKER pointing to the built md2svg worker; no API calls"]
async fn real_renderer_reuses_process_recovers_rejections_and_reclaims_timeout() -> Result<()> {
    let executable = std::env::var("MUTSUMI_RENDER_WORKER")?;
    let temp = tempfile::tempdir()?;
    let worker = RendererWorker::new(
        Path::new(&executable),
        &temp.path().join("normal"),
        Duration::from_secs(60),
    )
    .await?;
    let result: Result<()> = async {
        let first = worker
            .render("# 媒体链路\n\n中文 **加粗**，公式 $E=mc^2$。", 640)
            .await?;
        let bytes = std::fs::read(&first.png)?;
        let image = image::load_from_memory(&bytes)?.to_rgba8();
        assert_eq!(image.width(), first.width);
        assert_eq!(image.height(), first.height);
        assert!(
            image
                .pixels()
                .any(|p| p[0] < 200 && p[1] < 200 && p[2] < 200)
        );
        assert!(
            worker
                .render("<script>alert(1)</script>", 640)
                .await
                .is_err()
        );
        let second = worker.render("# 恢复成功\n\n普通 Markdown。", 640).await?;
        assert_eq!(first.worker_pid, second.worker_pid);
        assert_ne!(first.png, second.png);
        println!(
            "renderer pid={} dimensions={}x{}",
            first.worker_pid, first.width, first.height
        );
        Ok(())
    }
    .await;
    worker.shutdown().await?;
    result?;
    let tiny = RendererWorker::new(
        Path::new(&executable),
        &temp.path().join("timeout"),
        Duration::from_micros(1),
    )
    .await?;
    assert!(tiny.render("# Deadline", 640).await.is_err());
    tiny.shutdown().await?;
    assert!(
        std::fs::read_dir(temp.path().join("timeout"))?
            .next()
            .is_none()
    );
    Ok(())
}
