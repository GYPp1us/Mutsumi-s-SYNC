use anyhow::{Result, bail};
use chrono::{FixedOffset, Utc};
use serde_json::{Value, json};

/// The schema registry is the only model-facing tool inventory.
#[derive(Default)]
pub struct ToolRegistry;

impl ToolRegistry {
    pub fn schemas(&self) -> Vec<Value> {
        vec![
            json!({"type":"function","function":{"name":"clock","description":"读取可信宿主当前 UTC+8 时间。只读，无外部副作用。","parameters":{"type":"object","properties":{},"additionalProperties":false}}}),
        ]
    }
    pub fn validate(&self, name: &str, arguments: &Value) -> Result<()> {
        if name != "clock" {
            bail!("unknown tool")
        }
        if !arguments.as_object().is_some_and(|x| x.is_empty()) {
            bail!("clock arguments must be an empty object")
        }
        Ok(())
    }
    pub async fn execute(&self, name: &str, arguments: &Value) -> Result<Value> {
        self.validate(name, arguments)?;
        Ok(
            json!({"time":Utc::now().with_timezone(&FixedOffset::east_opt(8*3600).unwrap()).to_rfc3339(),"source":"host_clock"}),
        )
    }
}
