//! Authenticated loopback OneBot transport and a narrow NapCat login/status client.
use crate::{
    media::MediaService,
    protocol::OutgoingMessage,
    sender::{Receipt, Sender},
};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NapcatConfig {
    pub base_url: String,
    pub token_env: String,
    pub webui_url: String,
    pub webui_token_env: String,
    pub bot_id: String,
    pub owner_id: String,
    pub targets: BTreeMap<String, Peer>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Peer {
    pub kind: String,
    pub id: String,
}

#[derive(Debug)]
pub struct NotStarted(pub &'static str);
impl std::fmt::Display for NotStarted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for NotStarted {}

pub struct NapcatClient {
    pub config: NapcatConfig,
    http: reqwest::Client,
    token: String,
    web_token: String,
    credential: Mutex<Option<(Instant, String)>>,
}
impl NapcatClient {
    pub fn new(config: NapcatConfig) -> Result<Self> {
        for url in [&config.base_url, &config.webui_url] {
            let url = reqwest::Url::parse(url)?;
            if url.scheme() != "http"
                || !matches!(
                    url.host_str(),
                    Some("127.0.0.1" | "localhost" | "[::1]" | "::1")
                )
            {
                bail!("NapCat must use authenticated loopback HTTP")
            }
        }
        if [&config.bot_id, &config.owner_id]
            .iter()
            .any(|s| s.is_empty() || !s.chars().all(|c| c.is_ascii_digit()))
            || config.targets.values().any(|p| {
                !matches!(p.kind.as_str(), "private" | "group")
                    || p.id.is_empty()
                    || !p.id.chars().all(|c| c.is_ascii_digit())
            })
        {
            bail!("invalid NapCat identity binding")
        }
        let token = std::env::var(&config.token_env).context("missing OneBot token")?;
        let web_token =
            std::env::var(&config.webui_token_env).context("missing NapCat admin token")?;
        if token.len() < 16 || web_token.is_empty() {
            bail!("invalid NapCat credentials")
        }
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .build()?;
        Ok(Self {
            config,
            http,
            token,
            web_token,
            credential: Mutex::new(None),
        })
    }
    pub async fn api(&self, action: &str, params: Value) -> Result<Value> {
        let response = self
            .http
            .post(format!(
                "{}/{}",
                self.config.base_url.trim_end_matches('/'),
                action
            ))
            .bearer_auth(&self.token)
            .json(&params)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("NapCat API transport unavailable"))?;
        if !response.status().is_success() {
            bail!("NapCat API HTTP {}", response.status().as_u16())
        }
        let body: Value = response.json().await.context("invalid NapCat response")?;
        if body["status"] != "ok" || body["retcode"] != 0 {
            bail!("NapCat API did not confirm success")
        }
        Ok(body["data"].clone())
    }
    pub async fn web_api(&self, action: &str, params: Value) -> Result<Value> {
        let mut guard = self.credential.lock().await;
        for attempt in 0..2 {
            if guard
                .as_ref()
                .is_none_or(|(at, _)| at.elapsed() > Duration::from_secs(45 * 60))
            {
                let hash = format!("{:x}", Sha256::digest(format!("{}.napcat", self.web_token)));
                let response: Value = self
                    .http
                    .post(format!(
                        "{}/api/auth/login",
                        self.config.webui_url.trim_end_matches('/')
                    ))
                    .json(&json!({"hash":hash}))
                    .send()
                    .await
                    .map_err(|_| anyhow::anyhow!("NapCat panel unavailable"))?
                    .json()
                    .await?;
                let credential = response["data"]["Credential"]
                    .as_str()
                    .context("NapCat panel authentication failed")?;
                *guard = Some((Instant::now(), credential.to_owned()));
            }
            let credential = &guard.as_ref().unwrap().1;
            let body: Value = self
                .http
                .post(format!(
                    "{}/api/{}",
                    self.config.webui_url.trim_end_matches('/'),
                    action
                ))
                .bearer_auth(credential)
                .json(&params)
                .send()
                .await
                .map_err(|_| anyhow::anyhow!("NapCat panel request failed"))?
                .json()
                .await?;
            if body["code"] == 0 {
                return Ok(body["data"].clone());
            }
            *guard = None;
            if attempt == 1 {
                bail!("NapCat panel action unavailable")
            }
        }
        unreachable!()
    }
    pub async fn status(&self) -> Value {
        let (web, onebot) = tokio::join!(
            self.web_api("QQLogin/CheckLoginStatus", json!({})),
            self.api("get_status", json!({}))
        );
        json!({"panel_available":web.is_ok(),"logged_in":web.as_ref().is_ok_and(|v|v["isLogin"]==true),"offline":web.as_ref().is_ok_and(|v|v["isOffline"]==true),"has_qrcode":web.as_ref().is_ok_and(|v|v["qrcodeurl"].as_str().is_some_and(|s|!s.is_empty())),"onebot_available":onebot.is_ok(),"onebot_online":onebot.as_ref().ok().map(|v|v["online"].clone())})
    }
    pub async fn qr_svg(&self) -> Result<String> {
        let value = self.web_api("QQLogin/GetQQLoginQrcode", json!({})).await?;
        let content = value["qrcode"].as_str().context("no login QR available")?;
        if content.len() > 4096 {
            bail!("QR content exceeds budget")
        }
        Ok(qrcode::QrCode::new(content.as_bytes())?
            .render::<qrcode::render::svg::Color>()
            .min_dimensions(280, 280)
            .build())
    }
    pub async fn image_bytes(&self, file: &str, url: Option<&str>) -> Result<Vec<u8>> {
        let resolved = if let Some(url) = url.filter(|s| !s.is_empty()) {
            url.to_owned()
        } else {
            self.api("get_image", json!({"file":file})).await?["url"]
                .as_str()
                .context("image has no remote URL")?
                .to_owned()
        };
        let mut url = reqwest::Url::parse(&resolved)?;
        let host = url.host_str().context("missing image host")?;
        if !["qpic.cn", "qq.com", "gtimg.cn"]
            .iter()
            .any(|suffix| host == *suffix || host.ends_with(&format!(".{suffix}")))
            || !matches!(url.scheme(), "http" | "https")
        {
            bail!("unsupported QQ image source")
        }
        url.set_scheme("https")
            .map_err(|_| anyhow::anyhow!("invalid image scheme"))?;
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        crate::simulator::bounded_bytes(
            client
                .get(url)
                .send()
                .await
                .map_err(|_| anyhow::anyhow!("QQ image download unavailable"))?,
        )
        .await
    }
}

pub struct LiveSender {
    pub ledger: crate::ledger::Ledger,
    pub media: Arc<MediaService>,
    pub napcat: Option<Arc<NapcatClient>>,
    pub qq_enabled: Arc<AtomicBool>,
    pub directory: crate::protocol::Directory,
    pub bindings: BTreeMap<String, Peer>,
    pub session: String,
}
#[async_trait]
impl Sender for LiveSender {
    async fn send(&self, id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        let target = message.target_ref.as_deref().context("missing target")?;
        let binding = self
            .directory
            .targets
            .get(target)
            .context("unknown target")?;
        if binding.transport == "webui" {
            self.ledger
                .event(
                    &self.session,
                    id,
                    "web_message",
                    json!({"message_id":format!("web:{id}"),"message":message}),
                )
                .await?;
            return Ok(Receipt {
                transport: "webui_store".into(),
                message_id: format!("web:{id}"),
                message: message.clone(),
            });
        }
        if !self.qq_enabled.load(Ordering::Acquire) {
            return Err(NotStarted("QQ receiving/sending is disabled by the owner").into());
        }
        let client = self
            .napcat
            .as_ref()
            .ok_or(NotStarted("NapCat is not configured"))?;
        let peer = self
            .bindings
            .get(target)
            .ok_or(NotStarted("target has no QQ binding"))?;
        let array = crate::onebot::outbound_array(message, &self.media).await?;
        let mut params = json!({"message":array,"auto_escape":false});
        let action = if peer.kind == "private" {
            params["user_id"] = json!(peer.id);
            "send_private_msg"
        } else {
            params["group_id"] = json!(peer.id);
            "send_group_msg"
        };
        let result = client.api(action, params).await?;
        let message_id = result["message_id"]
            .as_i64()
            .map(|n| n.to_string())
            .or_else(|| result["message_id"].as_str().map(str::to_owned))
            .context("NapCat did not return a message id")?;
        Ok(Receipt {
            transport: "napcat_onebot".into(),
            message_id,
            message: message.clone(),
        })
    }
}
