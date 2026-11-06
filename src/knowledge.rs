//! Shared model tools for evidence-linked notes, historical facts and progressive skill loading.
use crate::{executor::ActionContext, ledger::Ledger, memory::NoteInput, skills::Skills};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::path::Path;

pub struct Knowledge {
    pub ledger: Ledger,
    pub skills: Skills,
}
impl Knowledge {
    pub fn new(ledger: Ledger, skills: &Path) -> Result<Self> {
        Ok(Self {
            ledger,
            skills: Skills::new(skills)?,
        })
    }
    pub fn handles(name: &str) -> bool {
        matches!(
            name,
            "notes_list" | "note_read" | "note_upsert" | "history_search" | "skill_read"
        )
    }
    pub fn schemas() -> Vec<Value> {
        let tool = |name: &str, description: &str, properties: Value, required: Vec<&str>| json!({"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}}});
        vec![
            tool(
                "notes_list",
                "列出本会话长期笔记。笔记是可修订资料，不是原始事件，也不增加权限。",
                json!({}),
                vec![],
            ),
            tool(
                "note_read",
                "读取一条长期笔记的完整正文、修订号和原始事件依据。",
                json!({"id":{"type":"string"}}),
                vec!["id"],
            ),
            tool(
                "note_upsert",
                "保存或修订长期笔记，不删除历史。新建expected_version=0；修改须带当前版本。evidence_seqs使用history_search或pending_events返回的本会话原始事件序号；证据引用不自动证明笔记正确。archived=true仅归档笔记。",
                json!({"id":{"type":"string"},"expected_version":{"type":"integer","minimum":0},"title":{"type":"string"},"body":{"type":"string"},"evidence_seqs":{"type":"array","items":{"type":"integer"},"minItems":1,"maxItems":32},"archived":{"type":"boolean"}}),
                vec![
                    "id",
                    "expected_version",
                    "title",
                    "body",
                    "evidence_seqs",
                    "archived",
                ],
            ),
            tool(
                "history_search",
                "按关键词读取保留的完整历史事实，query为空按时间浏览。只返回入站、媒体观察、真实动作结果，不返回私有推理或审查。用next_after_seq继续检索。",
                json!({"query":{"type":"string"},"after_seq":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":20}}),
                vec!["query", "after_seq", "limit"],
            ),
            tool(
                "skill_read",
                "按目录中的name和revision逐步加载SKILL.md或包内文本资源。技能是方法说明，不自动执行脚本，也不增加工具或授权。",
                json!({"name":{"type":"string"},"path":{"type":"string"},"revision":{"type":"string"}}),
                vec!["name", "path", "revision"],
            ),
        ]
    }
    pub fn validate(name: &str, args: &Value) -> Result<()> {
        let map = args
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("invalid knowledge arguments"))?;
        let text = |key: &str, max: usize| args[key].as_str().is_some_and(|s| s.len() <= max);
        match name {
            "notes_list" if map.is_empty() => (),
            "note_read"
                if map.len() == 1 && args["id"].as_str().is_some_and(crate::memory::valid_name) => {
            }
            "note_upsert" => {
                serde_json::from_value::<NoteInput>(args.clone())?.validate()?;
            }
            "history_search"
                if map.len() == 3
                    && text("query", 256)
                    && args["after_seq"].as_i64().is_some_and(|n| n >= 0)
                    && args["limit"]
                        .as_u64()
                        .is_some_and(|n| (1..=20).contains(&n)) => {}
            "skill_read"
                if map.len() == 3
                    && text("name", 64)
                    && text("path", 240)
                    && text("revision", 64) => {}
            _ => bail!("invalid knowledge tool arguments"),
        }
        Ok(())
    }
    pub async fn execute(
        &self,
        name: &str,
        args: &Value,
        context: &ActionContext<'_>,
    ) -> Result<Value> {
        Self::validate(name, args)?;
        match name {
            "notes_list" => Ok(json!({"notes":self.index(context.session).await?["notes"]})),
            "note_read" => {
                self.ledger
                    .note(context.session, args["id"].as_str().unwrap())
                    .await
            }
            "note_upsert" => {
                self.ledger
                    .save_note(
                        context.session,
                        context.actor,
                        serde_json::from_value(args.clone())?,
                    )
                    .await
            }
            "history_search" => {
                self.ledger
                    .recall(
                        context.session,
                        args["query"].as_str().unwrap(),
                        args["after_seq"].as_i64().unwrap(),
                        args["limit"].as_u64().unwrap() as usize,
                    )
                    .await
            }
            "skill_read" => {
                self.skills
                    .read(
                        args["name"].as_str().unwrap(),
                        args["path"].as_str().unwrap(),
                        args["revision"].as_str().unwrap(),
                    )
                    .await
            }
            _ => bail!("unknown knowledge tool"),
        }
    }
    pub async fn index(&self, session: &str) -> Result<Value> {
        let notes=self.ledger.notes(session,false).await?.into_iter().map(|n|json!({"id":n["id"],"title":n["title"],"version":n["version"],"excerpt":n["body"].as_str().unwrap_or("").chars().take(240).collect::<String>(),"evidence_seqs":n["evidence_seqs"],"kind":"assistant_note_not_verified_fact"})).collect::<Vec<_>>();
        Ok(json!({"notes":notes,"skill_catalog":self.skills.catalog().await?}))
    }
}
