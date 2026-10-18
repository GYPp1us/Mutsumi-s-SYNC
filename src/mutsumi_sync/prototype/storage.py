"""Durable background jobs alongside the existing event ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ..memory.store import MessageStore


logger = logging.getLogger("mutsumi.prototype.store")
_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled", "unknown"})
_UPDATE_FIELDS = frozenset({"input_event_ids", "updates", "status", "result", "delivered", "active_action"})


@dataclass
class BackgroundJob:
    id: str
    conversation_id: str
    actor_id: str
    peer_chat_type: int
    peer_uid: str
    request: str
    input_event_ids: list[str]
    updates: list[str]
    status: str
    result: str
    delivered: bool
    created_at: float
    active_action: str = ""

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> BackgroundJob:
        return cls(
            id=row["id"],
            conversation_id=row["conversation_id"],
            actor_id=row["actor_id"],
            peer_chat_type=row["peer_chat_type"],
            peer_uid=row["peer_uid"],
            request=row["request"],
            input_event_ids=json.loads(row["input_event_ids"]),
            updates=json.loads(row["updates"]),
            status=row["status"],
            result=row["result"],
            delivered=bool(row["delivered"]),
            created_at=row["created_at"],
            active_action=row["active_action"],
        )


def _string_list(value: Any, field: str) -> str:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return json.dumps(value, ensure_ascii=False)


class PrototypeStore(MessageStore):
    """Use a separate prototype database; inherit event and inner-journal APIs."""

    async def initialize(self) -> None:
        await super().initialize()
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS prototype_jobs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                peer_chat_type INTEGER NOT NULL,
                peer_uid TEXT NOT NULL,
                request TEXT NOT NULL,
                request_key TEXT NOT NULL UNIQUE,
                input_event_ids TEXT NOT NULL DEFAULT '[]',
                updates TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'unknown')),
                result TEXT NOT NULL DEFAULT '',
                delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0, 1)),
                created_at REAL NOT NULL,
                active_action TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_prototype_jobs_conversation
                ON prototype_jobs(conversation_id, created_at);
        """)
        await self._conn.commit()

    async def create_job(
        self,
        *,
        conversation_id: str,
        actor_id: str,
        peer_chat_type: int,
        peer_uid: str,
        request: str,
        input_event_ids: list[str],
    ) -> BackgroundJob:
        self._ensure_initialized()
        if not all(isinstance(value, str) and value.strip() for value in (
            conversation_id, actor_id, peer_uid, request,
        )):
            raise ValueError("job source, recipient, and request must be nonblank strings")
        if type(peer_chat_type) is not int or peer_chat_type not in (1, 2):
            raise ValueError("peer_chat_type must be 1 (private) or 2 (group)")
        encoded_ids = _string_list(input_event_ids, "input_event_ids")
        job_id = str(uuid.uuid4())
        request_key = hashlib.sha256(json.dumps(
            [conversation_id, input_event_ids], ensure_ascii=False,
        ).encode("utf-8")).hexdigest() if input_event_ids else job_id
        await self._conn.execute(
            "INSERT INTO prototype_jobs "
            "(id, conversation_id, actor_id, peer_chat_type, peer_uid, request, request_key, input_event_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_key) DO NOTHING",
            (job_id, conversation_id, actor_id, peer_chat_type, peer_uid, request,
             request_key, encoded_ids, time.time()),
        )
        await self._conn.commit()
        cursor = await self._conn.execute(
            "SELECT * FROM prototype_jobs WHERE request_key = ?", (request_key,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("created job cannot be read")
        job = BackgroundJob.from_row(row)
        logger.info("Background job queued id=%s conversation=%s", job.id, conversation_id)
        return job

    async def get_job(self, id: str) -> BackgroundJob | None:
        self._ensure_initialized()
        cursor = await self._conn.execute("SELECT * FROM prototype_jobs WHERE id = ?", (id,))
        row = await cursor.fetchone()
        return BackgroundJob.from_row(row) if row else None

    async def list_jobs(self, conversation_id: str | None = None) -> list[BackgroundJob]:
        self._ensure_initialized()
        query = "SELECT * FROM prototype_jobs"
        params: tuple[str, ...] = ()
        if conversation_id is not None:
            query += " WHERE conversation_id = ?"
            params = (conversation_id,)
        cursor = await self._conn.execute(query + " ORDER BY created_at, rowid", params)
        return [BackgroundJob.from_row(row) for row in await cursor.fetchall()]

    async def update_job(self, id: str, **fields: Any) -> BackgroundJob:
        """Update mutable work state while keeping the source and recipient fixed."""
        self._ensure_initialized()
        invalid = fields.keys() - _UPDATE_FIELDS
        if invalid:
            raise ValueError("unsupported job fields: " + ", ".join(sorted(invalid)))
        values: list[Any] = []
        for field, value in fields.items():
            if field in {"input_event_ids", "updates"}:
                value = _string_list(value, field)
            elif field == "status":
                if not isinstance(value, str) or value not in _STATUSES:
                    raise ValueError("invalid job status")
            elif field == "delivered":
                if type(value) is not bool:
                    raise ValueError("delivered must be a bool")
                value = int(value)
            elif not isinstance(value, str):
                raise ValueError(f"{field} must be a string")
            values.append(value)
        if fields:
            assignments = ", ".join(f"{field} = ?" for field in fields)
            await self._conn.execute(
                f"UPDATE prototype_jobs SET {assignments} WHERE id = ?", (*values, id)
            )
            await self._conn.commit()
        job = await self.get_job(id)
        if job is None:
            raise KeyError(f"unknown background job: {id}")
        return job

    async def recover_jobs(self) -> list[BackgroundJob]:
        """Mark interrupted work uncertain and retain every undelivered report.

        Only queued jobs can start automatically. An interrupted running job may
        already have performed its active action, so recovery never replays it.
        """
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE prototype_jobs SET status = 'unknown' WHERE status = 'running'"
        )
        await self._conn.commit()
        if cursor.rowcount:
            logger.warning("Recovered %s interrupted background jobs as unknown", cursor.rowcount)
        cursor = await self._conn.execute(
            "SELECT * FROM prototype_jobs WHERE status = 'queued' OR delivered = 0 "
            "ORDER BY created_at, rowid"
        )
        return [BackgroundJob.from_row(row) for row in await cursor.fetchall()]
