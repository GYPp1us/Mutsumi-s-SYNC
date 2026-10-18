use crate::protocol::{Directory, OutgoingMessage, validate_message};
use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Receipt {
    pub transport: String,
    pub message_id: String,
    pub message: OutgoingMessage,
}

#[async_trait]
pub trait Sender: Send + Sync {
    async fn send(&self, action_id: &str, message: &OutgoingMessage) -> Result<Receipt>;
}

/// A verified stdout write, explicitly not a QQ receipt. Segment order is preserved.
pub struct TerminalSender(pub Directory);

#[async_trait]
impl Sender for TerminalSender {
    async fn send(&self, action_id: &str, message: &OutgoingMessage) -> Result<Receipt> {
        use std::io::Write;
        validate_message(message, &self.0)?;
        let receipt = Receipt {
            transport: "terminal".into(),
            message_id: format!("terminal:{action_id}"),
            message: message.clone(),
        };
        let line = serde_json::to_string(&receipt)?;
        // One short terminal I/O; no external send is claimed by this adapter.
        let mut stdout = std::io::stdout().lock();
        writeln!(stdout, "{line}")?;
        stdout.flush()?;
        Ok(receipt)
    }
}
