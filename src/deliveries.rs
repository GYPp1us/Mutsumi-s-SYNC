//! Durable internal deliveries. This queue never retries an external send.
use crate::ledger::Ledger;
use anyhow::Result;
use rusqlite::{OptionalExtension, Transaction, params};
use serde_json::{Value, json};

pub(crate) fn enqueue(tx: &Transaction<'_>, kind: &str, key: &str, payload: &Value) -> Result<()> {
    tx.execute(
        "INSERT INTO deliveries(kind,key,payload) VALUES(?,?,?)
         ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload,generation=generation+1,
         status='pending',lease=NULL,attempts=0,due_ms=0,error=''",
        params![kind, key, payload.to_string()],
    )?;
    Ok(())
}

#[derive(Clone)]
pub struct DeliveryQueue(pub Ledger);

pub struct Claim {
    pub kind: String,
    pub key: String,
    pub payload: Value,
    pub attempts: u32,
    generation: i64,
    lease: String,
}

impl DeliveryQueue {
    pub async fn enqueue(&self, kind: &str, key: &str, payload: Value) -> Result<()> {
        let (kind, key) = (kind.to_owned(), key.to_owned());
        self.0
            .db(move |db| {
                let tx = db.transaction()?;
                enqueue(&tx, &kind, &key, &payload)?;
                tx.commit()?;
                Ok(())
            })
            .await
    }
    pub async fn claim(&self, kind: &str) -> Result<Option<Claim>> {
        let kind = kind.to_owned();
        self.0.db(move |db| {
            let tx=db.transaction()?;
            let row:Option<(String,String,i64,u32)>=tx.query_row(
                "SELECT key,payload,generation,attempts FROM deliveries WHERE kind=? AND status='pending' AND due_ms<=? ORDER BY due_ms,rowid LIMIT 1",
                params![kind,chrono::Utc::now().timestamp_millis()], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
            let Some((key,payload,generation,attempts))=row else {return Ok(None)};
            let lease=uuid::Uuid::new_v4().to_string();
            tx.execute("UPDATE deliveries SET status='running',lease=? WHERE kind=? AND key=?",params![lease,kind,key])?;
            tx.commit()?;
            Ok(Some(Claim{kind,key,payload:serde_json::from_str(&payload)?,generation,lease,attempts}))
        }).await
    }
    /// Compare both generation and lease: a stale completion cannot erase a new payload.
    pub async fn complete(
        &self,
        claim: Claim,
        event: Option<(String, String, String, Value)>,
    ) -> Result<bool> {
        self.0.db(move |db| {
            let tx=db.transaction()?;
            let changed=tx.execute("DELETE FROM deliveries WHERE kind=? AND key=? AND generation=? AND lease=? AND status='running'",
                params![claim.kind,claim.key,claim.generation,claim.lease])?==1;
            if changed && let Some((session,turn,kind,payload))=event {
                tx.execute("INSERT INTO events(session,turn_id,kind,payload) VALUES(?,?,?,?)",params![session,turn,kind,payload.to_string()])?;
                if matches!(kind.as_str(),"inbound"|"media_observation") {enqueue(&tx,"attention",&session,&json!({"session":session}))?;}
            }
            tx.commit()?;Ok(changed)
        }).await
    }
    pub async fn retry(&self, claim: Claim, reason: &str) -> Result<()> {
        let reason = reason.chars().take(256).collect::<String>();
        let delay = (1000_i64 * 2_i64.pow(claim.attempts.min(6))).min(60_000);
        self.0.db(move |db| {
            db.execute("UPDATE deliveries SET status='pending',lease=NULL,attempts=attempts+1,due_ms=?,error=? WHERE kind=? AND key=? AND generation=? AND lease=?",
                params![chrono::Utc::now().timestamp_millis()+delay,reason,claim.kind,claim.key,claim.generation,claim.lease])?;
            Ok(())
        }).await
    }
    pub async fn summary(&self) -> Result<Vec<Value>> {
        self.0.db(|db| {
            let rows=db.prepare("SELECT kind,status,COUNT(*),MAX(attempts) FROM deliveries GROUP BY kind,status")?
                .query_map([],|r|Ok(json!({"kind":r.get::<_,String>(0)?,"status":r.get::<_,String>(1)?,"count":r.get::<_,i64>(2)?,"attempts":r.get::<_,i64>(3)?})))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            Ok(rows)
        }).await
    }
}
