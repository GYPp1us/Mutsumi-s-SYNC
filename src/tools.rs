use crate::media::MediaService;
use anyhow::{Result, bail};
use chrono::{FixedOffset, Utc};
use serde_json::{Value, json};
use std::sync::Arc;

/// The schema registry is the only model-facing tool inventory.
#[derive(Default)]
pub struct ToolRegistry {
    pub media: Option<Arc<MediaService>>,
    pub workspace: Option<Arc<crate::workspace::WorkspaceTools>>,
}

impl ToolRegistry {
    pub fn schemas(&self) -> Vec<Value> {
        let mut schemas = vec![
            json!({"type":"function","function":{"name":"clock","description":"读取可信宿主当前 UTC+8 时间。只读，无外部副作用。","parameters":{"type":"object","properties":{},"additionalProperties":false}}}),
        ];
        if self.media.is_some() {
            schemas.extend([
            json!({"type":"function","function":{"name":"media_search","description":"按真实视觉描述搜索已有媒体。query为空可列出；target_ref明确选择预期听众，只返回该听众有权访问的引用。不会重新识图或发送。","parameters":{"type":"object","properties":{"query":{"type":"string","description":"描述中的关键词，可为空"},"target_ref":{"type":"string"}},"required":["query","target_ref"],"additionalProperties":false}}}),
            json!({"type":"function","function":{"name":"render_markdown","description":"将Markdown、支持的LaTeX与Mermaid在独立md2svg/resvg进程中渲染为实际图片，返回真实media_id；不发送。不能加载外部图片、HTML或本地路径。target_ref明确绑定预期听众，后续发送仍需独立审查。","parameters":{"type":"object","properties":{"markdown":{"type":"string"},"width":{"type":"integer","minimum":256,"maximum":1200},"target_ref":{"type":"string"}},"required":["markdown","width","target_ref"],"additionalProperties":false}}}),
        ]);
        }
        if self.workspace.is_some() {
            schemas.extend(crate::workspace::WorkspaceTools::schemas());
        }
        schemas
    }
    pub fn validate(&self, name: &str, arguments: &Value) -> Result<()> {
        let map = arguments
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("tool arguments must be an object"))?;
        match name {
            "bash" | "edit" | "wait" if self.workspace.is_some() => {
                return crate::workspace::WorkspaceTools::validate(name, arguments);
            }
            "clock" if map.is_empty() => (),
            "media_search"
                if self.media.is_some()
                    && map.len() == 2
                    && map
                        .get("target_ref")
                        .and_then(Value::as_str)
                        .is_some_and(|t| self.media.as_ref().unwrap().known_target(t))
                    && map
                        .get("query")
                        .and_then(Value::as_str)
                        .is_some_and(|q| q.len() <= 256) => {}
            "render_markdown"
                if self.media.is_some()
                    && map.len() == 3
                    && map
                        .get("markdown")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.trim().is_empty() && s.len() <= 32 * 1024)
                    && map
                        .get("width")
                        .and_then(Value::as_u64)
                        .is_some_and(|w| (256..=1200).contains(&w))
                    && map
                        .get("target_ref")
                        .and_then(Value::as_str)
                        .is_some_and(|t| self.media.as_ref().unwrap().known_target(t)) => {}
            _ => bail!("unknown tool or invalid arguments"),
        }
        Ok(())
    }
    pub async fn execute(
        &self,
        name: &str,
        arguments: &Value,
        source_target: &str,
    ) -> Result<Value> {
        self.execute_with_cancel(
            name,
            arguments,
            source_target,
            &tokio_util::sync::CancellationToken::new(),
        )
        .await
    }
    pub async fn execute_with_cancel(
        &self,
        name: &str,
        arguments: &Value,
        source_target: &str,
        cancel: &tokio_util::sync::CancellationToken,
    ) -> Result<Value> {
        self.validate(name, arguments)?;
        match name {
            "bash" | "edit" | "wait" => {
                self.workspace
                    .as_ref()
                    .unwrap()
                    .execute(name, arguments, cancel)
                    .await
            }
            "media_search" => {
                self.media
                    .as_ref()
                    .unwrap()
                    .search(
                        arguments["query"].as_str().unwrap(),
                        arguments["target_ref"].as_str().unwrap(),
                    )
                    .await
            }
            "render_markdown" => {
                self.media
                    .as_ref()
                    .unwrap()
                    .render(
                        arguments["markdown"].as_str().unwrap(),
                        arguments["width"].as_u64().unwrap() as u32,
                        source_target,
                        arguments["target_ref"].as_str().unwrap(),
                    )
                    .await
            }
            _ => Ok(
                json!({"time":Utc::now().with_timezone(&FixedOffset::east_opt(8*3600).unwrap()).to_rfc3339(),"source":"host_clock"}),
            ),
        }
    }
}
