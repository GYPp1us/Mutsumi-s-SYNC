//! Editable, evidence-linked notes; original events remain the historical record.
use crate::ledger::Ledger;
use anyhow::{Result, bail};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NoteInput {
    pub id: String,
    pub expected_version: u64,
    pub title: String,
    pub body: String,
    pub evidence_seqs: Vec<i64>,
    pub archived: bool,
}
impl NoteInput {
    pub fn validate(&self) -> Result<()> {
        if !valid_name(&self.id)
            || self.title.trim().is_empty()
            || self.title.len() > 240
            || self.body.trim().is_empty()
            || self.body.len() > 8192
            || self.evidence_seqs.is_empty()
            || self.evidence_seqs.len() > 32
            || self.evidence_seqs.iter().any(|s| *s <= 0)
        {
            bail!("invalid note; provide title, body and 1–32 original event sequence references")
        }
        Ok(())
    }
}
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_'))
}
impl Ledger {
    pub async fn notes(&self, session: &str, include_archived: bool) -> Result<Vec<Value>> {
        let session = session.to_owned();
        self.db(move |db| {
            let rows: Vec<String> = db
                .prepare("SELECT document FROM notes WHERE session=? ORDER BY id")?
                .query_map([session], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            let notes = rows
                .into_iter()
                .map(|s| Ok(serde_json::from_str::<Value>(&s)?))
                .collect::<Result<Vec<_>>>()?;
            Ok(notes
                .into_iter()
                .filter(|n| include_archived || n["archived"] != true)
                .collect())
        })
        .await
    }
    pub async fn note(&self, session: &str, id: &str) -> Result<Value> {
        let (session, id) = (session.to_owned(), id.to_owned());
        self.db(move |db| {
            let raw: String = db.query_row(
                "SELECT document FROM notes WHERE session=? AND id=?",
                params![session, id],
                |r| r.get(0),
            )?;
            Ok(serde_json::from_str(&raw)?)
        })
        .await
    }
    pub async fn save_note(&self, session: &str, author: &str, input: NoteInput) -> Result<Value> {
        input.validate()?;
        let (session, author) = (session.to_owned(), author.to_owned());
        self.db(move |db| {
            let tx=db.transaction()?;
            let old:Option<u64>=tx.query_row("SELECT version FROM notes WHERE session=? AND id=?",params![session,input.id],|r|r.get(0)).optional()?;
            if old.unwrap_or(0)!=input.expected_version {bail!("note version changed; reload before editing")}
            let count:i64=tx.query_row("SELECT COUNT(*) FROM notes WHERE session=?",[&session],|r|r.get(0))?;
            if old.is_none() && count>=128 {bail!("note count limit reached")}
            for seq in &input.evidence_seqs {
                let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM events WHERE session=? AND seq=? AND kind IN ('inbound','media_observation','action_result'))",params![session,seq],|r|r.get(0))?;
                if !valid {bail!("note evidence must refer to original events in this session")}
            }
            let version=input.expected_version.checked_add(1).ok_or_else(||anyhow::anyhow!("note revision exhausted"))?;
            let note=json!({"id":input.id,"version":version,"title":input.title,"body":input.body,"evidence_seqs":input.evidence_seqs,"archived":input.archived,"author":author,"updated_at":chrono::Utc::now().to_rfc3339(),"kind":"assistant_note_not_verified_fact"});
            tx.execute("INSERT INTO notes VALUES(?,?,?,?) ON CONFLICT(session,id) DO UPDATE SET version=excluded.version,document=excluded.document",params![session,input.id,version,note.to_string()])?;
            tx.execute("INSERT INTO note_versions VALUES(?,?,?,?)",params![session,input.id,version,note.to_string()])?;
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES(?,'notes','knowledge_changed',?)",params![session,json!({"note_id":input.id,"version":version,"author":author}).to_string()])?;
            tx.commit()?;Ok(note)
        }).await
    }
    /// Only externally meaningful facts are returned, never private prompts/reviews/raw transport.
    pub async fn recall(
        &self,
        session: &str,
        query: &str,
        after: i64,
        limit: usize,
    ) -> Result<Value> {
        if query.len() > 256 || after < 0 || !(1..=20).contains(&limit) {
            bail!("invalid history query")
        }
        let (session, query) = (session.to_owned(), query.to_owned());
        self.db(move |db| {
            let rows:Vec<(i64,String,String)>=db.prepare("SELECT seq,kind,payload FROM events WHERE session=? AND seq>? AND kind IN ('inbound','media_observation','action_result') ORDER BY seq LIMIT 1000")?
                .query_map(params![session,after],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?.collect::<rusqlite::Result<_>>()?;
            let mut result=Vec::new();let mut scanned=after;let mut bytes=0;
            for (seq,kind,raw) in rows {
                let data:Value=serde_json::from_str(&raw)?;
                let data=if kind=="inbound"{crate::onebot::public_inbound(data)}else{data};
                let text=data.to_string();
                if !text.to_lowercase().contains(&query.to_lowercase()) {scanned=seq;continue;}
                if bytes+text.len()>64*1024 && !result.is_empty() {break;}
                bytes+=text.len();scanned=seq;
                result.push(json!({"seq":seq,"kind":kind,"data":data}));
                if result.len()==limit {break;}
            }
            Ok(json!({"events":result,"next_after_seq":scanned,"complete_history_retained":true}))
        }).await
    }
}
