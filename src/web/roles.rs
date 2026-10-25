//! Host-owned QQ identities. Relationship edits never rename an existing audience.
use crate::{
    napcat::{NapcatConfig, Peer},
    protocol::{Directory, Target},
    scheduler::Source,
};
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Owner,
    Friend,
    Stranger,
    Blocked,
}
impl Role {
    pub fn relationship(self) -> &'static str {
        match self {
            Self::Owner => "owner",
            Self::Friend => "friend",
            Self::Stranger => "stranger",
            Self::Blocked => "blocked",
        }
    }
}
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    #[default]
    Owner,
    Registered,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Contact {
    pub qq_id: String,
    pub name: String,
    pub role: Role,
    pub enabled: bool,
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RoleBook {
    pub version: u64,
    pub scope: Scope,
    // The legacy audience remains bound to this exact account after owner changes.
    pub legacy_owner_id: String,
    pub contacts: BTreeMap<String, Contact>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Update {
    pub version: u64,
    pub scope: Scope,
    pub contacts: Vec<Contact>,
}

impl RoleBook {
    pub fn seed(config: Option<&NapcatConfig>) -> Self {
        let Some(config) = config else {
            return Self::default();
        };
        let contact = Contact {
            qq_id: config.owner_id.clone(),
            name: "主人".into(),
            role: Role::Owner,
            enabled: true,
        };
        Self {
            version: 1,
            scope: Scope::Owner,
            legacy_owner_id: contact.qq_id.clone(),
            contacts: BTreeMap::from([(contact.qq_id.clone(), contact)]),
        }
    }
    pub fn target(&self, id: &str) -> String {
        if id == self.legacy_owner_id {
            "qq_owner".into()
        } else {
            format!("qq_private_{id}")
        }
    }
    pub fn allows(&self, contact: &Contact) -> bool {
        contact.enabled
            && contact.role != Role::Blocked
            && (self.scope == Scope::Registered || contact.role == Role::Owner)
    }
    pub fn source(&self, contact: &Contact) -> Source {
        let target = self.target(&contact.qq_id);
        Source {
            actor: format!("qq:{}", contact.qq_id),
            kind: "human".into(),
            relationship: contact.role.relationship().into(),
            conversation: target.clone(),
            reply_target: Some(target),
        }
    }
    pub fn validate(&self, bot_id: &str) -> Result<()> {
        if self.contacts.is_empty()
            || self.contacts.len() > 128
            || self
                .contacts
                .values()
                .filter(|c| c.role == Role::Owner)
                .count()
                != 1
        {
            bail!("必须保留且仅保留一个主人，联系人最多128个")
        }
        for (id, c) in &self.contacts {
            if id != &c.qq_id
                || id.is_empty()
                || id.len() > 20
                || id.starts_with('0')
                || !id.chars().all(|x| x.is_ascii_digit())
                || id == bot_id
                || c.name.trim().is_empty()
                || c.name.len() > 240
                || (c.role == Role::Owner && !c.enabled)
            {
                bail!("QQ号、称呼或主人启用状态无效")
            }
        }
        Ok(())
    }
    pub fn updated(&self, update: Update, bot_id: &str) -> Result<Self> {
        if update.version != self.version {
            bail!("角色配置已变化，请重新加载后再保存")
        }
        let mut seen = BTreeSet::new();
        if update
            .contacts
            .iter()
            .any(|c| !seen.insert(c.qq_id.clone()))
        {
            bail!("QQ号不能重复")
        }
        let book = Self {
            version: self
                .version
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("角色版本溢出"))?,
            scope: update.scope,
            legacy_owner_id: self.legacy_owner_id.clone(),
            contacts: update
                .contacts
                .into_iter()
                .map(|c| (c.qq_id.clone(), c))
                .collect(),
        };
        book.validate(bot_id)?;
        Ok(book)
    }
    pub fn directory(&self, base: &Directory, bot_id: &str, include_inactive: bool) -> Directory {
        let mut directory = base.clone();
        directory.targets.retain(|_, t| t.transport != "napcat");
        for contact in self
            .contacts
            .values()
            .filter(|c| include_inactive || self.allows(c))
        {
            directory.targets.insert(
                self.target(&contact.qq_id),
                Target {
                    label: format!("{} · QQ私聊", contact.name),
                    account: bot_id.into(),
                    audience: format!("{}:{}", contact.role.relationship(), contact.qq_id),
                    transport: "napcat".into(),
                },
            );
        }
        directory
    }
    pub fn bindings(&self) -> BTreeMap<String, Peer> {
        self.contacts
            .values()
            .filter(|c| self.allows(c))
            .map(|c| {
                (
                    self.target(&c.qq_id),
                    Peer {
                        kind: "private".into(),
                        id: c.qq_id.clone(),
                    },
                )
            })
            .collect()
    }
}
