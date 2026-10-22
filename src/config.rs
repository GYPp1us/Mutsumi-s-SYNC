use crate::protocol::Directory;
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::{path::Path, time::Duration};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    pub base_url: String,
    pub model: String,
    pub api_key_env: String,
    pub timeout_seconds: u64,
    pub max_tokens: u32,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub provider: ProviderConfig,
    pub database: String,
    pub prompts: String,
    pub quick_replies: String,
    pub max_rounds: usize,
    pub max_corrections: usize,
    pub directory: Directory,
    #[serde(default)]
    pub jev: Option<crate::jev::JevConfig>,
    #[serde(default)]
    pub media: Option<MediaConfig>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaConfig {
    pub root: String,
    pub worker_executable: String,
    pub worker_timeout_seconds: u64,
    pub outbox: String,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prompts {
    pub persona: String,
    pub runtime: String,
    pub reviewer: String,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
        if !config.provider.base_url.starts_with("https://") {
            bail!("provider requires HTTPS");
        }
        if !(1..=32).contains(&config.max_rounds)
            || config.max_corrections > 4
            || !(1..=180).contains(&config.provider.timeout_seconds)
            || !(256..=8192).contains(&config.provider.max_tokens)
        {
            bail!("invalid runtime budgets")
        }
        if config.directory.targets.is_empty()
            || config
                .directory
                .targets
                .values()
                .any(|t| t.transport != "terminal")
        {
            bail!("this prototype requires explicit terminal targets");
        }
        Ok(config)
    }
    pub fn prompts(&self) -> Result<Prompts> {
        let p: Prompts =
            serde_yaml::from_str(&std::fs::read_to_string(&self.prompts).context("read prompts")?)?;
        if [&p.runtime, &p.persona, &p.reviewer]
            .iter()
            .any(|x| x.trim().is_empty())
        {
            bail!("all prompt sections must be non-empty");
        }
        Ok(p)
    }
}

impl ProviderConfig {
    pub fn timeout(&self) -> Duration {
        Duration::from_secs(self.timeout_seconds)
    }
}
