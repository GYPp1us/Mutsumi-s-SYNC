//! Small workspace tools. Bash runs in a separate filesystem/PID namespace on Linux.
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::{Value, json};
#[cfg(target_os = "linux")]
use std::process::Stdio;
use std::{
    path::{Component, Path, PathBuf},
    time::Duration,
};
use tokio::sync::Mutex;
#[cfg(target_os = "linux")]
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::Command,
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceConfig {
    pub root: String,
}

pub struct WorkspaceTools {
    pub root: PathBuf,
    edit_lock: Mutex<()>,
}

#[derive(Debug)]
pub struct UnknownEffect(pub Value);
impl std::fmt::Display for UnknownEffect {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "command stopped after starting; effects may have occurred"
        )
    }
}
impl std::error::Error for UnknownEffect {}

impl WorkspaceTools {
    pub async fn new(config: &WorkspaceConfig) -> Result<Self> {
        tokio::fs::create_dir_all(&config.root).await?;
        Ok(Self {
            root: tokio::fs::canonicalize(&config.root).await?,
            edit_lock: Mutex::new(()),
        })
    }
    pub fn schemas() -> Vec<Value> {
        vec![
            json!({"type":"function","function":{"name":"bash","description":"在独立工作目录的 Linux 沙箱执行 bash。没有模型 key、宿主文件、网络或 /proc 进程视图；可读系统工具，只能写工作目录。超时/取消回收整个子进程组，副作用可能已发生，不自动重试。输出截断不代表命令失败。","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":120}},"required":["command","timeout_seconds"],"additionalProperties":false}}}),
            json!({"type":"function","function":{"name":"edit","description":"在工作目录内精确编辑 UTF-8 文件。old_text 必须恰好出现一次；old_text 为空仅用于新建不存在的文件。相对路径，不接受目录逃逸或符号链接。","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["path","old_text","new_text"],"additionalProperties":false}}}),
            json!({"type":"function","function":{"name":"wait","description":"异步等待指定秒数，期间入口仍可接收新事件；结束后模型读取最新上下文。不能安排未来定时任务。","parameters":{"type":"object","properties":{"seconds":{"type":"integer","minimum":1,"maximum":120}},"required":["seconds"],"additionalProperties":false}}}),
        ]
    }
    pub fn validate(name: &str, args: &Value) -> Result<()> {
        let map = args.as_object().context("tool arguments must be object")?;
        let valid = match name {
            "bash" => {
                map.len() == 2
                    && args["command"]
                        .as_str()
                        .is_some_and(|s| !s.trim().is_empty() && s.len() <= 16 * 1024)
                    && args["timeout_seconds"]
                        .as_u64()
                        .is_some_and(|s| (1..=120).contains(&s))
            }
            "edit" => {
                map.len() == 3
                    && args["path"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 256)
                    && ["old_text", "new_text"]
                        .iter()
                        .all(|k| args[k].as_str().is_some_and(|s| s.len() <= 256 * 1024))
            }
            "wait" => {
                map.len() == 1
                    && args["seconds"]
                        .as_u64()
                        .is_some_and(|s| (1..=120).contains(&s))
            }
            _ => false,
        };
        if !valid {
            bail!("invalid workspace tool arguments")
        };
        Ok(())
    }
    pub async fn execute(
        &self,
        name: &str,
        args: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value> {
        Self::validate(name, args)?;
        match name {
            "edit" => {
                self.edit(
                    args["path"].as_str().unwrap(),
                    args["old_text"].as_str().unwrap(),
                    args["new_text"].as_str().unwrap(),
                )
                .await
            }
            "bash" => {
                self.bash(
                    args["command"].as_str().unwrap(),
                    args["timeout_seconds"].as_u64().unwrap(),
                    cancel,
                )
                .await
            }
            _ => {
                tokio::select! {_=cancel.cancelled()=>bail!("wait cancelled"),_=tokio::time::sleep(Duration::from_secs(args["seconds"].as_u64().unwrap()))=>Ok(json!({"waited_seconds":args["seconds"]}))}
            }
        }
    }
    pub async fn edit(&self, path: &str, old: &str, new: &str) -> Result<Value> {
        let _lock = self.edit_lock.lock().await;
        let relative = Path::new(path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
        {
            bail!("edit requires a confined relative path")
        }
        let file = self.root.join(relative);
        let parent = file.parent().context("missing parent")?;
        let parent = tokio::fs::canonicalize(parent)
            .await
            .context("parent directory must already exist")?;
        if !parent.starts_with(&self.root) {
            bail!("edit parent escaped workspace")
        }
        let exists = tokio::fs::try_exists(&file).await?;
        if let Ok(metadata) = tokio::fs::symlink_metadata(&file).await
            && (metadata.file_type().is_symlink() || !metadata.is_file())
        {
            bail!("edit refuses symlinks and non-files")
        }
        let content = if exists {
            if old.is_empty() {
                bail!("empty old_text only creates new files")
            }
            if tokio::fs::metadata(&file).await?.len() > 512 * 1024 {
                bail!("file exceeds edit budget")
            }
            let text = tokio::fs::read_to_string(&file).await?;
            if text.matches(old).count() != 1 {
                bail!("old_text must match exactly once")
            }
            text.replacen(old, new, 1)
        } else {
            if !old.is_empty() {
                bail!("file does not exist")
            };
            new.to_owned()
        };
        if content.len() > 512 * 1024 {
            bail!("edited file exceeds budget")
        }
        let temporary = parent.join(format!(".sync-edit-{}", uuid::Uuid::new_v4()));
        tokio::fs::write(&temporary, content.as_bytes()).await?;
        if let Err(error) = tokio::fs::rename(&temporary, &file).await {
            let _ = tokio::fs::remove_file(&temporary).await;
            return Err(error.into());
        }
        Ok(json!({"path":path,"bytes":content.len(),"created":!exists}))
    }
    #[cfg(not(target_os = "linux"))]
    async fn bash(
        &self,
        _command: &str,
        _seconds: u64,
        _cancel: &CancellationToken,
    ) -> Result<Value> {
        bail!("bash sandbox requires Linux")
    }

    #[cfg(target_os = "linux")]
    async fn bash(&self, command: &str, seconds: u64, cancel: &CancellationToken) -> Result<Value> {
        use std::os::unix::process::CommandExt;
        let mut process = Command::new("/usr/bin/bwrap");
        process
            .env_clear()
            .env("PATH", "/usr/local/bin:/usr/bin:/bin")
            .env("HOME", "/workspace")
            .env("LANG", "C.UTF-8");
        process.args([
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
        ]);
        for path in [
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc/alternatives",
            "/etc/ssl/certs",
        ] {
            if Path::new(path).exists() {
                process.args(["--ro-bind", path, path]);
            }
        }
        process
            .args([
                // A fresh procfs cannot be mounted inside systemd's protected proc view.
                // An empty filesystem also prevents access to host process environments.
                "--tmpfs", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind",
            ])
            .arg(&self.root)
            .args([
                "/workspace",
                "--chdir",
                "/workspace",
                "--",
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                command,
            ]);
        process
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        process.as_std_mut().process_group(0);
        let mut child = process.spawn().context("start bash sandbox")?;
        let pid = child.id().context("missing bash process id")?;
        let stdout = tokio::spawn(read_bounded(child.stdout.take().unwrap()));
        let stderr = tokio::spawn(read_bounded(child.stderr.take().unwrap()));
        let started = std::time::Instant::now();
        let (exit, interrupted) = tokio::select! {
            result=child.wait()=>(Some(result?),None),
            _=cancel.cancelled()=>(None,Some("cancelled")),
            _=tokio::time::sleep(Duration::from_secs(seconds))=>(None,Some("timed_out")),
        };
        // A command cannot leave background processes running after the tool returns.
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
        if interrupted.is_some() {
            let _ = child.wait().await;
        }
        let (out, out_cut) = stdout.await??;
        let (err, err_cut) = stderr.await??;
        let value = json!({"exit_code":exit.and_then(|s|s.code()),"stdout":out,"stderr":err,"output_truncated":out_cut||err_cut,"elapsed_ms":started.elapsed().as_millis(),"status":interrupted.unwrap_or("exited")});
        if interrupted.is_some() {
            return Err(UnknownEffect(value).into());
        }
        Ok(value)
    }
}

#[cfg(target_os = "linux")]
async fn read_bounded(mut stream: impl AsyncRead + Unpin) -> Result<(String, bool)> {
    let mut kept = Vec::new();
    let mut buffer = [0_u8; 4096];
    let mut cut = false;
    loop {
        let n = stream.read(&mut buffer).await?;
        if n == 0 {
            break;
        }
        let room = (16 * 1024_usize).saturating_sub(kept.len());
        kept.extend_from_slice(&buffer[..n.min(room)]);
        cut |= n > room;
    }
    Ok((String::from_utf8_lossy(&kept).into_owned(), cut))
}
