//! File-backed SKILL.md packages. Loading instructions never grants executable capabilities.
use anyhow::{Result, bail};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex},
};

#[derive(Clone)]
struct Skill {
    name: String,
    description: String,
    revision: String,
    files: BTreeMap<String, String>,
}
#[derive(Default)]
struct Snapshot {
    skills: BTreeMap<String, Skill>,
    warning: Option<String>,
}
#[derive(Clone)]
pub struct Skills {
    root: PathBuf,
    state: Arc<Mutex<Snapshot>>,
}
#[derive(Deserialize)]
struct Header {
    name: String,
    description: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Edit {
    pub path: String,
    pub content: String,
    pub expected_revision: Option<String>,
}

fn relative(path: &str) -> Result<PathBuf> {
    if path.is_empty() || path.len() > 240 || path.contains(['\\', ':', '\0']) {
        bail!("invalid skill resource path")
    }
    let p = Path::new(path);
    if p.components().any(|c| !matches!(c, Component::Normal(_))) {
        bail!("skill resource path must stay inside its package")
    }
    Ok(p.to_owned())
}
fn package(name: &str, files: BTreeMap<String, String>) -> Result<Skill> {
    if !crate::memory::valid_name(name)
        || files.len() > 64
        || files.values().map(String::len).sum::<usize>() > 256 * 1024
    {
        bail!("invalid skill package or size")
    }
    let manifest = files
        .get("SKILL.md")
        .ok_or_else(|| anyhow::anyhow!("SKILL.md is required"))?;
    let normalized = manifest.replace("\r\n", "\n");
    let head = normalized
        .strip_prefix("---\n")
        .and_then(|s| s.split_once("\n---\n"))
        .ok_or_else(|| anyhow::anyhow!("SKILL.md requires YAML frontmatter and instructions"))?;
    let header: Header = serde_yaml::from_str(head.0)?;
    if header.name != name
        || header.description.trim().is_empty()
        || header.description.len() > 1024
        || head.1.trim().is_empty()
    {
        bail!("skill name, description or instructions are invalid")
    }
    let revision = format!("{:x}", Sha256::digest(serde_json::to_vec(&files)?));
    Ok(Skill {
        name: name.into(),
        description: header.description,
        revision,
        files,
    })
}
fn collect(
    root: &Path,
    base: &Path,
    files: &mut BTreeMap<String, String>,
    depth: usize,
) -> Result<()> {
    if depth > 4 || files.len() > 64 {
        bail!("skill resource limits exceeded")
    }
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        if ty.is_symlink() {
            bail!("skill symlinks are not allowed")
        }
        if ty.is_dir() {
            collect(&entry.path(), base, files, depth + 1)?;
        } else if ty.is_file() {
            if entry.metadata()?.len() > 64 * 1024 {
                bail!("skill resource exceeds 64 KiB")
            }
            let path = entry.path();
            let name = path
                .strip_prefix(base)?
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("invalid skill resource name"))?
                .replace('\\', "/");
            relative(&name)?;
            files.insert(name, std::fs::read_to_string(path)?);
        } else {
            bail!("unsupported skill resource")
        }
    }
    Ok(())
}
impl Skills {
    pub fn new(root: &Path) -> Result<Self> {
        std::fs::create_dir_all(root)?;
        if std::fs::symlink_metadata(root)?.file_type().is_symlink() {
            bail!("skill root must not be a symlink")
        }
        let service = Self {
            root: root.canonicalize()?,
            state: Arc::new(Mutex::new(Snapshot::default())),
        };
        service.scan()?;
        Ok(service)
    }
    fn scan(&self) -> Result<()> {
        let mut snapshot = self
            .state
            .lock()
            .map_err(|_| anyhow::anyhow!("skill lock poisoned"))?;
        let candidate = (|| -> Result<BTreeMap<String, Skill>> {
            let mut skills = BTreeMap::new();
            for entry in std::fs::read_dir(&self.root)? {
                let entry = entry?;
                let name = entry
                    .file_name()
                    .to_str()
                    .ok_or_else(|| anyhow::anyhow!("invalid skill name"))?
                    .to_owned();
                if entry.file_type()?.is_symlink() || !entry.file_type()?.is_dir() {
                    bail!("skill root must contain package directories")
                }
                let mut files = BTreeMap::new();
                collect(&entry.path(), &entry.path(), &mut files, 0)?;
                skills.insert(name.clone(), package(&name, files)?);
                if skills.len() > 64 {
                    bail!("at most 64 skills are supported")
                }
            }
            Ok(skills)
        })();
        match candidate {
            Ok(skills) => {
                snapshot.skills = skills;
                snapshot.warning = None
            }
            Err(_) => {
                snapshot.warning = Some(
                    "技能目录存在无效配置；继续使用上一份有效快照，请检查文件格式与路径。".into(),
                )
            }
        }
        Ok(())
    }
    pub async fn catalog(&self) -> Result<Value> {
        let this = self.clone();
        tokio::task::spawn_blocking(move||{
            this.scan()?;
            let snapshot=this.state.lock().map_err(|_|anyhow::anyhow!("skill lock poisoned"))?;
            Ok(json!({"skills":snapshot.skills.values().map(|s|json!({"name":s.name,"description":s.description,"revision":s.revision,"resources":s.files.keys().collect::<Vec<_>>()})).collect::<Vec<_>>(),"warning":snapshot.warning}))
        }).await?
    }
    pub async fn read(&self, name: &str, path: &str, revision: &str) -> Result<Value> {
        relative(path)?;
        self.catalog().await?;
        let state = self
            .state
            .lock()
            .map_err(|_| anyhow::anyhow!("skill lock poisoned"))?;
        let skill = state
            .skills
            .get(name)
            .ok_or_else(|| anyhow::anyhow!("unknown skill"))?;
        if skill.revision != revision {
            bail!("skill revision changed; reload the catalog")
        }
        let content = skill
            .files
            .get(path)
            .ok_or_else(|| anyhow::anyhow!("unknown skill resource"))?;
        Ok(
            json!({"name":name,"path":path,"revision":skill.revision,"content":content,"authority":"reference instructions; tools and authorization remain host-owned"}),
        )
    }
    pub async fn edit(&self, name: &str, edit: Edit) -> Result<Value> {
        if !crate::memory::valid_name(name) || edit.content.len() > 64 * 1024 {
            bail!("invalid skill edit")
        }
        let relative = relative(&edit.path)?;
        let name = name.to_owned();
        let this = self.clone();
        tokio::task::spawn_blocking(move || {
            this.scan()?;
            let mut state = this
                .state
                .lock()
                .map_err(|_| anyhow::anyhow!("skill lock poisoned"))?;
            if state.warning.is_some() {
                bail!("repair invalid skill files before editing")
            }
            let old = state.skills.get(&name);
            if old.map(|s| s.revision.as_str()) != edit.expected_revision.as_deref() {
                bail!("skill revision changed; reload before saving")
            }
            let mut files = old.map(|s| s.files.clone()).unwrap_or_default();
            files.insert(edit.path.clone(), edit.content.clone());
            let skill = package(&name, files)?;
            if old.is_none() && state.skills.len() >= 64 {
                bail!("skill count limit reached")
            }
            let root = this.root.join(&name);
            let path = root.join(relative);
            let mut ancestor = path.parent();
            while let Some(dir) = ancestor {
                if dir.exists() && std::fs::symlink_metadata(dir)?.file_type().is_symlink() {
                    bail!("skill symlinks are not allowed")
                }
                if dir == this.root {
                    break;
                }
                ancestor = dir.parent();
            }
            if path.exists() && std::fs::symlink_metadata(&path)?.file_type().is_symlink() {
                bail!("skill symlinks are not allowed")
            }
            std::fs::create_dir_all(path.parent().unwrap())?;
            let temporary = path.with_file_name(format!(".{}.tmp", uuid::Uuid::new_v4()));
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            use std::io::Write;
            file.write_all(edit.content.as_bytes())?;
            file.sync_all()?;
            drop(file);
            std::fs::rename(&temporary, &path)?;
            #[cfg(unix)]
            std::fs::File::open(path.parent().unwrap())?.sync_all()?;
            let revision = skill.revision.clone();
            state.skills.insert(name, skill);
            Ok(json!({"saved":true,"revision":revision}))
        })
        .await?
    }
}
