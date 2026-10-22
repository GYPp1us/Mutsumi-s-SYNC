use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{Mutex, Semaphore},
};

struct WorkerIo {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

pub struct RendererWorker {
    executable: PathBuf,
    root: PathBuf,
    timeout: Duration,
    state: Mutex<Option<WorkerIo>>,
    queue: Arc<Semaphore>,
}

#[derive(Deserialize)]
struct Output {
    id: String,
    svg_file: String,
    png_file: String,
    width: u32,
    height: u32,
}

pub struct Rendered {
    pub svg: PathBuf,
    pub png: PathBuf,
    pub width: u32,
    pub height: u32,
    pub worker_pid: u32,
}

impl RendererWorker {
    pub async fn new(executable: &Path, root: &Path, timeout: Duration) -> Result<Self> {
        if timeout.is_zero() || timeout > Duration::from_secs(120) {
            bail!("invalid worker timeout")
        }
        tokio::fs::create_dir_all(root).await?;
        Ok(Self {
            executable: executable.to_owned(),
            root: tokio::fs::canonicalize(root).await?,
            timeout,
            state: Mutex::new(None),
            queue: Arc::new(Semaphore::new(8)),
        })
    }
    fn spawn(&self) -> Result<WorkerIo> {
        let mut command = Command::new(&self.executable);
        command
            .arg("--root")
            .arg(&self.root)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true);
        #[cfg(windows)]
        {
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().context("render worker could not start")?;
        let input = child.stdin.take().context("missing worker stdin")?;
        let output = BufReader::new(child.stdout.take().context("missing worker stdout")?);
        Ok(WorkerIo {
            child,
            input,
            output,
        })
    }
    pub async fn render(&self, markdown: &str, width: u32) -> Result<Rendered> {
        if markdown.len() > 32 * 1024 || !(256..=1200).contains(&width) {
            bail!("render input exceeds budget")
        }
        let _slot = self
            .queue
            .clone()
            .try_acquire_owned()
            .context("render queue full")?;
        let mut state = tokio::time::timeout(Duration::from_secs(5), self.state.lock())
            .await
            .context("render queue wait exceeded")?;
        if state.is_none() {
            *state = Some(self.spawn()?)
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let request = json!({"id":id,"markdown":markdown,"width":width}).to_string();
        let io = state.as_mut().unwrap();
        let pid = io.child.id().unwrap_or(0);
        let exchange = async {
            io.input.write_all(request.as_bytes()).await?;
            io.input.write_all(b"\n").await?;
            io.input.flush().await?;
            let mut bytes = Vec::new();
            (&mut io.output)
                .take(16 * 1024 + 1)
                .read_until(b'\n', &mut bytes)
                .await?;
            if bytes.is_empty() || bytes.len() > 16 * 1024 {
                bail!("invalid worker response size")
            }
            let response: serde_json::Value = serde_json::from_slice(&bytes)?;
            if response["ok"] != true {
                return Ok(None);
            }
            let output: Output = serde_json::from_value(response["output"].clone())?;
            if output.id != id
                || output.svg_file != format!("{id}.svg")
                || output.png_file != format!("{id}.png")
                || u64::from(output.width) * u64::from(output.height) > 16_000_000
            {
                bail!("worker returned invalid artifact references")
            }
            Ok::<_, anyhow::Error>(Some(Rendered {
                svg: self.root.join(output.svg_file),
                png: self.root.join(output.png_file),
                width: output.width,
                height: output.height,
                worker_pid: pid,
            }))
        };
        match tokio::time::timeout(self.timeout, exchange).await {
            Ok(Ok(Some(output))) => Ok(output),
            Ok(Ok(None)) => {
                for extension in ["svg", "png"] {
                    let _ =
                        tokio::fs::remove_file(self.root.join(format!("{id}.{extension}"))).await;
                }
                bail!("renderer rejected unsupported input")
            }
            _ => {
                if let Some(mut io) = state.take() {
                    let _ = io.child.kill().await;
                    let _ = io.child.wait().await;
                }
                // Remove only this host-generated request's partial artifacts.
                for extension in ["svg", "png"] {
                    let _ =
                        tokio::fs::remove_file(self.root.join(format!("{id}.{extension}"))).await;
                }
                bail!("render worker timed out or failed; process reclaimed")
            }
        }
    }
    pub async fn shutdown(&self) -> Result<()> {
        if let Some(mut io) = self.state.lock().await.take() {
            io.child.kill().await?;
            io.child.wait().await?;
        }
        Ok(())
    }
}
