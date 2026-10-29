//! Shared ingress, attention checkpoints, and persistent cache records.
use crate::ledger::Ledger;
use anyhow::{Result, bail};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

impl Ledger {
    pub async fn install_profile(&self, profile: Value) -> Result<()> {
        self.db(move|db|{
            let tx=db.transaction()?;
            let old:Option<String>=tx.query_row("SELECT value FROM cache WHERE namespace='runtime' AND key='profile'",[],|r|r.get(0)).optional()?;
            if old.as_deref()!=Some(&profile.to_string()) {
                tx.execute("INSERT INTO cache VALUES('runtime','profile',?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",[profile.to_string()])?;
                tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('runtime','profile','profile_changed',?)",[profile.to_string()])?;
            }
            tx.commit()?;Ok(())
        }).await
    }
    pub async fn describe_media(
        &self,
        id: &str,
        description: &str,
        observer: &str,
    ) -> Result<Value> {
        let (id, description, observer) =
            (id.to_owned(), description.to_owned(), observer.to_owned());
        self.db(move|db|{
            let tx=db.transaction()?;
            let raw:String=tx.query_row("SELECT record FROM media WHERE id=?",[&id],|r|r.get(0))?;
            let mut record:Value=serde_json::from_str(&raw)?;
            record["description"]=json!(description);record["origin"]=json!(format!("vision:{observer}"));
            tx.execute("UPDATE media SET record=? WHERE id=?",params![record.to_string(),id])?;
            tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES('media_catalog','observation','media_registered',?)",[json!({"media_id":id,"observer_model":observer,"description":description}).to_string()])?;
            tx.commit()?;Ok(record)
        }).await
    }
    pub async fn ingest(
        &self,
        session: &str,
        source: &str,
        id: &str,
        payload: Value,
    ) -> Result<(i64, bool)> {
        let (session, source, id) = (session.to_owned(), source.to_owned(), id.to_owned());
        self.db(move |db| {
            let tx = db.transaction()?;
            let result = Self::ingest_transaction(&tx, &session, &source, &id, &payload)?;
            tx.commit()?;
            Ok(result)
        })
        .await
    }
    pub(crate) fn ingest_transaction(
        tx: &rusqlite::Transaction<'_>,
        session: &str,
        source: &str,
        id: &str,
        payload: &Value,
    ) -> Result<(i64, bool)> {
        let existing: Option<i64> = tx
            .query_row(
                "SELECT seq FROM ingress_ids WHERE session=? AND source=? AND external_id=?",
                params![session, source, id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(seq) = existing {
            let original: String =
                tx.query_row("SELECT payload FROM events WHERE seq=?", [seq], |r| {
                    r.get(0)
                })?;
            let original: Value = serde_json::from_str(&original)?;
            if original["transport"] != payload["transport"] {
                bail!("ingress id reused with different transport payload")
            }
            return Ok((seq, false));
        }
        tx.execute(
            "INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,'inbound',?)",
            params![session, id, payload.to_string()],
        )?;
        let seq = tx.last_insert_rowid();
        tx.execute(
            "INSERT INTO ingress_ids VALUES(?,?,?,?)",
            params![session, source, id, seq],
        )?;
        Ok((seq, true))
    }
    pub async fn stream_events(&self, session: &str, after: i64) -> Result<Vec<Value>> {
        let session = session.to_owned();
        self.db(move|db|{
            let rows:Vec<(i64,String,String,String)>=db.prepare("SELECT seq,kind,payload,at FROM events WHERE session=? AND seq>? ORDER BY seq")?.query_map(params![session,after],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
            rows.into_iter().map(|(seq,kind,payload,at)|Ok(json!({"seq":seq,"kind":kind,"data":serde_json::from_str::<Value>(&payload)?,"at":at}))).collect()
        }).await
    }
    pub async fn pending(&self, session: &str) -> Result<Vec<Value>> {
        let after = self.cursor(session).await?;
        Ok(self
            .stream_events(session, after)
            .await?
            .into_iter()
            .filter(|e| matches!(e["kind"].as_str(), Some("inbound" | "media_observation")))
            .collect())
    }
    pub async fn stream_page(&self, session: &str, after: i64, limit: usize) -> Result<Vec<Value>> {
        let session = session.to_owned();
        let limit = limit.min(500) as i64;
        self.db(move|db|{
            let rows:Vec<(i64,String,String,String)>=db.prepare("SELECT seq,kind,payload,at FROM events WHERE session=? AND seq>? ORDER BY seq LIMIT ?")?.query_map(params![session,after,limit],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
            rows.into_iter().map(|(seq,kind,payload,at)|Ok(json!({"seq":seq,"kind":kind,"data":serde_json::from_str::<Value>(&payload)?,"at":at}))).collect()
        }).await
    }
    pub async fn cursor(&self, session: &str) -> Result<i64> {
        let session = session.to_owned();
        self.db(move |db| {
            Ok(db
                .query_row(
                    "SELECT through_seq FROM attention_cursors WHERE session=?",
                    [session],
                    |r| r.get(0),
                )
                .optional()?
                .unwrap_or(0))
        })
        .await
    }
    pub async fn attention(&self, session: &str, through: i64) -> Result<Value> {
        let cursor = self.cursor(session).await?;
        let events = self.stream_events(session, cursor).await?;
        let pending:Vec<Value>=events.into_iter().filter(|e|e["seq"].as_i64().is_some_and(|s|s<=through) && matches!(e["kind"].as_str(),Some("inbound"|"media_observation")))
            .map(|e|json!({"seq":e["seq"],"kind":e["kind"],"event_id":e["data"]["event_id"],"source_id":e["data"]["source_id"]})).collect();
        Ok(
            json!({"handled_through_seq":cursor,"context_through_seq":through,"pending_events":pending}),
        )
    }
    pub async fn checkpoint(&self, session: &str, through: i64) -> Result<()> {
        let session = session.to_owned();
        self.db(move|db|{db.execute("INSERT INTO attention_cursors VALUES(?,?) ON CONFLICT(session) DO UPDATE SET through_seq=MAX(through_seq,excluded.through_seq)",params![session,through])?;Ok(())}).await
    }
    pub async fn projected_through(&self, session: &str) -> Result<i64> {
        let session = session.to_owned();
        self.db(move|db|Ok(db.query_row("SELECT COALESCE(MAX(e.seq),0) FROM events e JOIN projected_events p ON p.seq=e.seq AND p.session=e.session WHERE e.session=? AND e.kind IN ('inbound','media_observation')",[session],|r|r.get(0))?)).await
    }
    pub async fn cache_get(&self, namespace: &str, key: &str) -> Result<Option<Value>> {
        let (namespace, key) = (namespace.to_owned(), key.to_owned());
        self.db(move |db| {
            let value: Option<String> = db
                .query_row(
                    "SELECT value FROM cache WHERE namespace=? AND key=?",
                    params![namespace, key],
                    |r| r.get(0),
                )
                .optional()?;
            Ok(value.map(|s| serde_json::from_str(&s)).transpose()?)
        })
        .await
    }
    pub async fn cache_put(&self, namespace: &str, key: &str, value: Value) -> Result<()> {
        let (namespace, key) = (namespace.to_owned(), key.to_owned());
        self.db(move|db|{db.execute("INSERT INTO cache VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",params![namespace,key,value.to_string()])?;Ok(())}).await
    }
}
