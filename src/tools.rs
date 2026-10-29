use crate::media::MediaService;
use anyhow::{Result, bail};
use chrono::{FixedOffset, Utc};
use serde_json::{Value, json};
use std::sync::Arc;

/// The schema registry is the only model-facing tool inventory.
#[derive(Clone, Default)]
pub struct ToolRegistry {
    pub events: Option<Arc<crate::reporting::EventRegistry>>,
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
        if self.events.is_some() {
            schemas.extend([
                json!({"type":"function","function":{"name":"register_event","description":"注册一个外部上报事件。只登记事件资料，不执行任务或自动回复；全部事件共用同一上报端点。返回事件名、地址、请求示例，调用凭据在WebUI外部上报页提供，不进入模型上下文。同名事件定义不可覆盖。","parameters":{"type":"object","properties":{"event":{"type":"string","pattern":"^[A-Za-z0-9._-]{1,96}$","description":"稳定事件名：1到96个ASCII字母、数字、点、下划线或连字符，例如market.alert"},"description":{"type":"string"},"source_kind":{"type":"string","enum":["service","email","child_agent"]}},"required":["event","description","source_kind"],"additionalProperties":false}}}),
                json!({"type":"function","function":{"name":"set_event_enabled","description":"启用或停用已经注册的外部上报事件。停用后拒绝新的上报，不删除已接收事实，也不自动重放。","parameters":{"type":"object","properties":{"event":{"type":"string","pattern":"^[A-Za-z0-9._-]{1,96}$","description":"稳定事件名：1到96个ASCII字母、数字、点、下划线或连字符，例如market.alert"},"enabled":{"type":"boolean"}},"required":["event","enabled"],"additionalProperties":false}}})
            ]);
        }
        schemas
    }
    pub fn validate(&self, name: &str, arguments: &Value) -> Result<()> {
        let map = arguments
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("tool arguments must be an object"))?;
        match name {
            "register_event" if self.events.is_some() => {
                let definition: crate::reporting::Definition =
                    serde_json::from_value(arguments.clone())?;
                return definition.validate();
            }
            "set_event_enabled"
                if self.events.is_some()
                    && map.len() == 2
                    && arguments["event"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 96)
                    && arguments["enabled"].is_boolean() => {}
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
            "register_event" => {
                self.events
                    .as_ref()
                    .unwrap()
                    .register(serde_json::from_value(arguments.clone())?)
                    .await
            }
            "set_event_enabled" => {
                self.events
                    .as_ref()
                    .unwrap()
                    .set_enabled(
                        arguments["event"].as_str().unwrap().into(),
                        arguments["enabled"].as_bool().unwrap(),
                    )
                    .await
            }
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
