use anyhow::Result;
use mutsumi_runtime::{
    deliveries::DeliveryQueue,
    ledger::Ledger,
    memory::NoteInput,
    skills::{Edit, Skills},
};
use serde_json::json;

#[tokio::test]
async fn internal_deliveries_survive_restart_and_stale_completion_cannot_delete_new_input()
-> Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("events.db");
    let ledger = Ledger::open(&path).await?;
    let queue = DeliveryQueue(ledger.clone());
    queue
        .enqueue("vision", "image", json!({"version":1}))
        .await?;
    let old = queue.claim("vision").await?.unwrap();
    queue
        .enqueue("vision", "image", json!({"version":2}))
        .await?;
    assert!(!queue.complete(old, None).await?);
    let interrupted = queue.claim("vision").await?.unwrap();
    assert_eq!(interrupted.payload["version"], 2);
    drop(queue);
    drop(ledger);
    let ledger = Ledger::open(&path).await?;
    let queue = DeliveryQueue(ledger.clone());
    let recovered = queue.claim("vision").await?.unwrap();
    assert_eq!(recovered.payload["version"], 2);
    assert!(!queue.complete(interrupted, None).await?);
    assert!(
        queue
            .complete(
                recovered,
                Some((
                    "s".into(),
                    "image".into(),
                    "media_observation".into(),
                    json!({"event_id":"image","observations":[]})
                ))
            )
            .await?
    );
    assert!(queue.claim("vision").await?.is_none());
    assert_eq!(ledger.stream_events("s", 0).await?.len(), 1);
    assert!(queue.claim("attention").await?.is_some());
    Ok(())
}

#[tokio::test]
async fn fresh_ingress_and_its_internal_delivery_commit_together_and_deduplicate() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let ledger = Ledger::open(&dir.path().join("events.db")).await?;
    let payload = json!({"event_id":"m1","transport":{"text":"original"}});
    let jobs = vec![("vision".into(), "m1".into(), json!({"media":["m1"]}))];
    let (seq, fresh) = ledger
        .ingest_with_jobs("s", "owner", "m1", payload.clone(), jobs.clone())
        .await?;
    assert!(fresh);
    let queue = DeliveryQueue(ledger.clone());
    let claim = queue.claim("vision").await?.unwrap();
    assert_eq!(
        ledger
            .ingest_with_jobs("s", "owner", "m1", payload, jobs)
            .await?,
        (seq, false)
    );
    assert!(queue.complete(claim, None).await?);
    assert!(queue.claim("vision").await?.is_none());
    Ok(())
}

#[tokio::test]
async fn notes_are_versioned_evidence_linked_and_never_replace_or_expose_raw_history() -> Result<()>
{
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("events.db");
    let ledger = Ledger::open(&path).await?;
    let seq=ledger.event("s","request","inbound",json!({"text":"只记住已确认的偏好","transport":{"key":"private-marker"},"segments":[{"type":"image","source_ref":"private-media-url"}]})).await?;
    let other = ledger
        .event(
            "other",
            "request",
            "inbound",
            json!({"text":"different owner"}),
        )
        .await?;
    let note = NoteInput {
        id: "preferences".into(),
        expected_version: 0,
        title: "偏好".into(),
        body: "使用中文；尚未执行操作".into(),
        evidence_seqs: vec![seq],
        archived: false,
    };
    let saved = ledger.save_note("s", "owner", note.clone()).await?;
    assert_eq!(saved["version"], 1);
    assert!(ledger.save_note("s", "owner", note.clone()).await.is_err());
    let mut invalid = note.clone();
    invalid.id = "other".into();
    invalid.evidence_seqs = vec![other];
    assert!(ledger.save_note("s", "owner", invalid).await.is_err());
    let mut update = note;
    update.expected_version = 1;
    update.archived = true;
    ledger.save_note("s", "owner", update).await?;
    assert!(ledger.notes("s", false).await?.is_empty());
    assert_eq!(ledger.notes("s", true).await?.len(), 1);
    let original = ledger.recall("s", "偏好", 0, 20).await?.to_string();
    assert!(!original.contains("private-marker"));
    assert!(!original.contains("private-media-url"));
    assert_eq!(
        ledger
            .stream_events("s", 0)
            .await?
            .iter()
            .filter(|e| e["kind"] == "inbound")
            .count(),
        1
    );
    drop(ledger);
    let ledger = Ledger::open(&path).await?;
    assert_eq!(ledger.note("s", "preferences").await?["version"], 2);
    Ok(())
}

#[tokio::test]
async fn skill_catalog_is_progressive_revisioned_and_keeps_last_valid_snapshot() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let service = Skills::new(dir.path())?;
    let manifest =
        "---\nname: sample\ndescription: 用于检查\n---\n\nprivate-full-instruction-marker\n";
    let result = service
        .edit(
            "sample",
            Edit {
                path: "SKILL.md".into(),
                content: manifest.into(),
                expected_revision: None,
            },
        )
        .await?;
    let revision = result["revision"].as_str().unwrap();
    let catalog = service.catalog().await?;
    assert!(
        !catalog
            .to_string()
            .contains("private-full-instruction-marker")
    );
    assert!(
        service
            .read("sample", "SKILL.md", revision)
            .await?
            .to_string()
            .contains("private-full-instruction-marker")
    );
    assert!(
        service
            .read("sample", "../outside", revision)
            .await
            .is_err()
    );
    assert!(service.read("sample", "SKILL.md", "stale").await.is_err());
    assert!(
        service
            .edit(
                "sample",
                Edit {
                    path: "SKILL.md".into(),
                    content: manifest.into(),
                    expected_revision: None
                }
            )
            .await
            .is_err()
    );
    let result = service
        .edit(
            "sample",
            Edit {
                path: "references/check.md".into(),
                content: "a reference".into(),
                expected_revision: Some(revision.into()),
            },
        )
        .await?;
    let latest = result["revision"].as_str().unwrap();
    std::fs::write(dir.path().join("sample/SKILL.md"), "broken YAML")?;
    assert!(service.catalog().await?["warning"].is_string());
    assert_eq!(
        service
            .read("sample", "references/check.md", latest)
            .await?["content"],
        "a reference"
    );
    Ok(())
}
