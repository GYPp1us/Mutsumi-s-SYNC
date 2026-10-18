use crate::protocol::{Action, AssistantRound};
use anyhow::{Context, Result, bail};
use fs2::FileExt;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    fs::File,
    path::Path,
    sync::{Arc, Mutex},
};

#[derive(Clone)]
pub struct Ledger {
    inner: Arc<Mutex<Connection>>,
    _process_lock: Arc<File>,
}

#[derive(Default)]
pub struct History {
    pub messages: Vec<Value>,
    pub event_ids: Vec<i64>,
}

impl Ledger {
    pub async fn open(path: &Path) -> Result<Self> {
        let path = path.to_owned();
        tokio::task::spawn_blocking(move || Self::open_sync(&path)).await?
    }

    fn open_sync(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let lock = File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("lock"))?;
        lock.try_lock_exclusive()
            .context("another process owns this prototype database")?;
        let mut connection = Connection::open(path)?;
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        let app_id: i64 = connection.pragma_query_value(None, "application_id", |r| r.get(0))?;
        let version: i64 = connection.pragma_query_value(None, "user_version", |r| r.get(0))?;
        let tables: i64 = connection.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            [],
            |r| r.get(0),
        )?;
        let empty = app_id == 0 && version == 0 && tables == 0;
        let supported = app_id == 0x4d535934 && version == 1;
        if !empty && !supported {
            bail!("database is not a supported V4 prototype ledger");
        }
        connection.execute_batch(include_str!("schema.sql"))?;
        Self::recover(&mut connection)?;
        Ok(Self {
            inner: Arc::new(Mutex::new(connection)),
            _process_lock: Arc::new(lock),
        })
    }

    fn recover(connection: &mut Connection) -> Result<()> {
        let tx = connection.transaction()?;
        let pending: Vec<(String, String, String)> = tx
            .prepare("SELECT id,session,turn_id FROM actions WHERE status='started'")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .collect::<rusqlite::Result<_>>()?;
        for (id, session, turn) in pending {
            let detail = json!({"action_id":id,"status":"unknown","reason":"process ended before a verified receipt"});
            tx.execute(
                "UPDATE actions SET status='unknown',result=? WHERE id=? AND status='started'",
                params![detail.to_string(), id],
            )?;
            tx.execute(
                "INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,'action_result',?)",
                params![session, turn, detail.to_string()],
            )?;
        }
        tx.execute(
            "INSERT INTO events(session,turn_id,kind,payload)
             SELECT e.session,e.turn_id,'turn_end','{\"status\":\"interrupted\"}'
             FROM events e WHERE e.kind='inbound'
             AND NOT EXISTS(SELECT 1 FROM events finished WHERE finished.turn_id=e.turn_id AND finished.kind='turn_end')",
            [],
        )?;
        tx.commit()?;
        Ok(())
    }

    async fn db<T: Send + 'static>(
        &self,
        f: impl FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    ) -> Result<T> {
        let inner = self.inner.clone();
        tokio::task::spawn_blocking(move || {
            let mut connection = inner
                .lock()
                .map_err(|_| anyhow::anyhow!("ledger mutex poisoned"))?;
            f(&mut connection)
        })
        .await?
    }

    pub async fn event(
        &self,
        session: &str,
        turn: &str,
        kind: &str,
        payload: Value,
    ) -> Result<i64> {
        let (session, turn, kind) = (session.to_owned(), turn.to_owned(), kind.to_owned());
        self.db(move |db| {
            db.execute(
                "INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,?,?)",
                params![session, turn, kind, payload.to_string()],
            )?;
            Ok(db.last_insert_rowid())
        })
        .await
    }

    pub async fn revision(&self) -> Result<i64> {
        self.db(|db| {
            Ok(db.query_row(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE kind IN ('inbound','action_result')",
                [],
                |r| r.get(0),
            )?)
        })
        .await
    }

    pub async fn facts(&self, session: &str) -> Result<Vec<Value>> {
        let session = session.to_owned();
        self.db(move |db| {
            let mut s=db.prepare("SELECT kind,payload FROM events WHERE session=? AND kind IN ('inbound','action_result') ORDER BY seq DESC LIMIT 16")?;
            let rows=s.query_map([session],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
            rows.into_iter().rev().map(|(kind,payload)|Ok(json!({"kind":kind,"data":serde_json::from_str::<Value>(&payload)?}))).collect()
        }).await
    }

    pub async fn snapshot(&self, session: &str) -> Result<(i64, Vec<Value>)> {
        let session = session.to_owned();
        self.db(move |db| {
            let revision=db.query_row("SELECT COALESCE(MAX(seq),0) FROM events WHERE kind IN ('inbound','action_result')",[],|r|r.get(0))?;
            let rows:Vec<(String,String)>=db.prepare("SELECT kind,payload FROM events WHERE session=? AND kind IN ('inbound','action_result') ORDER BY seq DESC LIMIT 16")?
                .query_map([session],|r|Ok((r.get(0)?,r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
            let facts=rows.into_iter().rev().map(|(kind,p)|Ok(json!({"kind":kind,"data":serde_json::from_str::<Value>(&p)?}))).collect::<Result<_>>()?;
            Ok((revision,facts))
        }).await
    }

    pub async fn review(
        &self,
        id: &str,
        revision: i64,
        fingerprint: &str,
        verdict: Value,
    ) -> Result<()> {
        let (id, fingerprint) = (id.to_owned(), fingerprint.to_owned());
        self.db(move |db| {
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?,?)",
                params![id, revision, fingerprint, verdict.to_string()],
            )?;
            Ok(())
        })
        .await
    }

    /// The transaction is the short execution boundary. No network I/O runs under it.
    pub async fn begin(
        &self,
        id: &str,
        session: &str,
        turn: &str,
        action: &Action,
        revision: i64,
    ) -> Result<()> {
        let (id, session, turn, action) = (
            id.to_owned(),
            session.to_owned(),
            turn.to_owned(),
            action.clone(),
        );
        self.db(move |db| {
            let tx = db.transaction()?;
            let current: i64 = tx.query_row(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE kind IN ('inbound','action_result')",
                [],
                |r| r.get(0),
            )?;
            if current != revision {
                bail!("new fact invalidated the review")
            }
            let reviewed: Option<(i64, String, String)> = tx
                .query_row(
                    "SELECT revision,fingerprint,verdict FROM reviews WHERE id=?",
                    [&id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            let Some((v, fp, verdict)) = reviewed else {
                bail!("missing review")
            };
            if v != revision
                || fp != action.fingerprint()
                || serde_json::from_str::<Value>(&verdict)?["decision"] != "allow"
            {
                bail!("review does not authorize these exact parameters")
            }
            tx.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,'started',NULL)",
                params![id, session, turn, serde_json::to_string(&action)?, revision],
            )?;
            tx.commit()?;
            Ok(())
        })
        .await
    }

    pub async fn finish(&self, id: &str, status: &str, result: Value) -> Result<i64> {
        let (id, status) = (id.to_owned(), status.to_owned());
        self.db(move |db| {
            let tx=db.transaction()?;
            let (session,turn,action):(String,String,String)=tx.query_row("SELECT session,turn_id,action FROM actions WHERE id=?",[&id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            if tx.execute("UPDATE actions SET status=?,result=? WHERE id=? AND status='started'",params![status,result.to_string(),id])?!=1 {bail!("action result already committed")}
            let data=json!({"action_id":id,"action":serde_json::from_str::<Value>(&action)?,"status":status,"result":result});
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,'action_result',?)",params![session,turn,data.to_string()])?;
            let seq=tx.last_insert_rowid();tx.commit()?;Ok(seq)
        }).await
    }

    pub async fn round(
        &self,
        id: &str,
        session: &str,
        turn: &str,
        round: &AssistantRound,
    ) -> Result<i64> {
        let (id, session, turn, round) = (
            id.to_owned(),
            session.to_owned(),
            turn.to_owned(),
            round.native(round.content.clone(), false),
        );
        self.db(move |db| {
            let tx = db.transaction()?;
            tx.execute(
                "INSERT INTO rounds(id,session,turn_id,assistant) VALUES(?,?,?,?)",
                params![id, session, turn, round.to_string()],
            )?;
            tx.execute(
                "INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,'model_round',?)",
                params![session, turn, json!({"round_id":id}).to_string()],
            )?;
            let seq = tx.last_insert_rowid();
            tx.commit()?;
            Ok(seq)
        })
        .await
    }

    pub async fn history(&self, session: &str) -> Result<History> {
        let session = session.to_owned();
        self.db(move |db| {
            let stored:Option<String>=db.query_row("SELECT messages FROM projections WHERE session=?",[&session],|r|r.get(0)).optional()?;
            let mut history=History {messages:stored.map(|s|serde_json::from_str(&s)).transpose()?.unwrap_or_default(),event_ids:vec![]};
            let rows:Vec<(i64,String,String)>=db.prepare("SELECT seq,kind,payload FROM events WHERE session=? AND seq NOT IN (SELECT seq FROM projected_events WHERE session=?) ORDER BY seq")?
                .query_map(params![session,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?.collect::<rusqlite::Result<_>>()?;
            for (seq,kind,payload) in rows {
                history.event_ids.push(seq);let data:Value=serde_json::from_str(&payload)?;
                match kind.as_str() {
                    "inbound" => history.messages.push(json!({"role":"user","content":data.to_string()})),
                    "action_result" => history.messages.push(json!({"role":"user","content":json!({"platform_observation":"recovered_action_result","data":data}).to_string()})),
                    "model_round" => {
                        let unfinished:Option<String>=db.query_row("SELECT assistant FROM rounds WHERE id=? AND finished=0",[data["round_id"].as_str().unwrap_or("")],|r|r.get(0)).optional()?;
                        if let Some(assistant)=unfinished {
                            let mut a:Value=serde_json::from_str(&assistant)?;a["content"]=Value::Null;
                            if let Some(calls)=a["tool_calls"].as_array().cloned() {
                                history.messages.push(a);
                                for call in calls {
                                    let prefix=format!("{}:tool:{}",data["round_id"].as_str().unwrap(),call["id"].as_str().unwrap_or(""));
                                    let found:Option<(String,Option<String>)>=db.query_row("SELECT status,result FROM actions WHERE id=?",[prefix],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                                    let outcome=match found {Some((status,result))=>json!({"status":status,"result":result.map(|r|serde_json::from_str::<Value>(&r)).transpose()?}),None=>json!({"status":"cancelled","reason":"not started before interruption"})};
                                    history.messages.push(json!({"role":"tool","tool_call_id":call["id"],"content":outcome.to_string()}));
                                }
                            }
                        }
                    },
                    _=>(),
                }
            }
            Ok(history)
        }).await
    }

    pub async fn save_history(
        &self,
        session: &str,
        history: &History,
        round_id: Option<&str>,
    ) -> Result<()> {
        let (session, messages, ids, round_id) = (
            session.to_owned(),
            serde_json::to_string(&history.messages)?,
            history.event_ids.clone(),
            round_id.map(str::to_owned),
        );
        self.db(move |db| {
            let tx=db.transaction()?;
            tx.execute("INSERT INTO projections VALUES(?,?) ON CONFLICT(session) DO UPDATE SET messages=excluded.messages",params![session,messages])?;
            for id in ids {tx.execute("INSERT OR IGNORE INTO projected_events VALUES(?,?)",params![session,id])?;}
            if let Some(id)=round_id {tx.execute("UPDATE rounds SET finished=1 WHERE id=?",[id])?;}
            tx.commit()?;Ok(())
        }).await
    }
}
