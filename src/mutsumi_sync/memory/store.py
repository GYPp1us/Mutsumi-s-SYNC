from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("mutsumi.store")

# These outcomes remain in the append-only ledger for audit and retry
# decisions, but they do not represent a conversational turn that the model
# should see or that an Episode may cover.
NON_PROJECTABLE_EVENT_STATUSES = frozenset({
    "cancelled",
    "error",
    "no_reply",
    "empty",
})

# These platform facts may be covered by an Episode after a conversational
# turn starts, but they must not block the coverage frontier when they occur
# between two Episodes. They have no ordinary provider-message projection.
COVERAGE_ONLY_EVENT_TYPES = frozenset({
    "media",
    "state_change",
    "status_update",
})


def _mime_for_extension(ext: str) -> str:
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "mp4": "video/mp4",
        "mp3": "audio/mpeg",
    }.get(ext.lower().lstrip("."), "application/octet-stream")


class MessageCategory(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    MIXED = "mixed"


class EventType(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MEDIA = "media"
    STATE_CHANGE = "state_change"
    STATUS_UPDATE = "status_update"


@dataclass
class EventRecord:
    """Immutable fact in the global event ledger."""

    conversation_id: str
    actor_id: str
    actor_kind: str
    event_type: str
    content: str = ""
    event_id: str | None = None
    actor_name: str = ""
    audience: str = "bot:self"
    visibility: str = "private"
    media_ids: list[str] | None = None
    payload: dict[str, Any] | None = None
    turn_id: str = ""
    pipeline_id: str = ""
    status: str = "finalized"
    created_at: float | None = None
    sequence: int | None = None

    def to_insert_values(self) -> tuple:
        return (
            self.event_id or str(uuid.uuid4()),
            self.conversation_id,
            self.actor_id,
            self.actor_kind,
            self.actor_name,
            self.event_type,
            self.content,
            json.dumps(self.media_ids or [], ensure_ascii=False),
            json.dumps(self.payload or {}, ensure_ascii=False),
            self.turn_id,
            self.pipeline_id,
            self.visibility,
            self.audience,
            self.status,
        )

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "EventRecord":
        return cls(
            event_id=row["event_id"],
            sequence=row["sequence"],
            conversation_id=row["conversation_id"],
            actor_id=row["actor_id"],
            actor_kind=row["actor_kind"],
            actor_name=row["actor_name"],
            event_type=row["event_type"],
            content=row["content"],
            media_ids=json.loads(row["media_ids_json"] or "[]"),
            payload=json.loads(row["payload_json"] or "{}") if "payload_json" in row.keys() else {},
            turn_id=row["turn_id"] if "turn_id" in row.keys() else "",
            pipeline_id=row["pipeline_id"],
            visibility=row["visibility"],
            audience=row["audience"],
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass
class EpisodeRecord:
    conversation_id: str
    first_sequence: int
    last_sequence: int
    narrative: str
    episode_id: str | None = None
    status: str = "ready"
    participants_json: str = "[]"
    open_loops: str = ""
    media_ids_json: str = "[]"
    created_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "EpisodeRecord":
        return cls(
            episode_id=row["episode_id"],
            conversation_id=row["conversation_id"],
            first_sequence=row["first_sequence"],
            last_sequence=row["last_sequence"],
            narrative=row["narrative"],
            status=row["status"],
            participants_json=row["participants_json"],
            open_loops=row["open_loops"],
            media_ids_json=row["media_ids_json"],
            created_at=row["created_at"],
        )


@dataclass
class MediaRecord:
    media_id: str
    sha256: str
    kind: str
    path: str = ""
    source_url: str = ""
    mime_type: str = ""
    description: str = ""
    short_description: str = ""
    status: str = "active"
    created_at: float | None = None
    last_used_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "MediaRecord":
        return cls(
            media_id=row["media_id"], sha256=row["sha256"], kind=row["kind"],
            path=row["path"], source_url=row["source_url"], mime_type=row["mime_type"],
            description=row["description"], short_description=row["short_description"],
            status=row["status"], created_at=row["created_at"], last_used_at=row["last_used_at"],
        )


@dataclass
class ActorRecord:
    actor_id: str
    kind: str
    platform: str = ""
    platform_subject_id: str = ""
    display_name: str = ""
    private_alias: str = ""
    relationship: str = ""
    metadata: dict[str, Any] | None = None
    created_at: float | None = None
    updated_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ActorRecord":
        keys = set(row.keys())
        return cls(
            actor_id=row["actor_id"],
            kind=row["kind"],
            platform=row["platform"] if "platform" in keys else "",
            platform_subject_id=row["platform_subject_id"] if "platform_subject_id" in keys else "",
            display_name=row["display_name"] if "display_name" in keys else "",
            private_alias=row["private_alias"] if "private_alias" in keys else "",
            relationship=row["relationship"] if "relationship" in keys else "",
            metadata=json.loads(row["metadata_json"] or "{}") if "metadata_json" in keys else {},
            created_at=row["created_at"] if "created_at" in keys else None,
            updated_at=row["updated_at"] if "updated_at" in keys else None,
        )


@dataclass
class StoredMessage:
    date: str
    group_key: str
    category: str
    content: str
    id: int | None = None
    created_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> StoredMessage:
        keys = set(row.keys())
        return cls(
            id=row["id"],
            date=row["date"],
            group_key=row["group_key"],
            category=row["category"],
            content=row["content"],
            created_at=row["created_at"] if "created_at" in keys else None,
        )


@dataclass
class InnerJournalRecord:
    id: int
    pipeline_id: str
    source_conversation_id: str
    source_actor_id: str
    source_event_ids: list[str]
    content: str
    status: str
    created_at: float | None = None


@dataclass
class ScheduledTaskRecord:
    id: int
    group_key: str
    peer_chat_type: int
    peer_uid: str
    prompt: str
    scheduled_at: float
    status: str
    created_at: float
    context: str = ""
    schedule_kind: str = "once"
    interval_seconds: float | None = None
    cron_expression: str = ""
    last_run_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> ScheduledTaskRecord:
        return cls(
            id=row["id"],
            group_key=row["group_key"],
            peer_chat_type=row["peer_chat_type"],
            peer_uid=row["peer_uid"],
            prompt=row["prompt"],
            context=str(row["context"] or "") if "context" in row.keys() else "",
            scheduled_at=row["scheduled_at"],
            status=row["status"],
            created_at=row["created_at"],
            schedule_kind=row["schedule_kind"] if "schedule_kind" in row.keys() else "once",
            interval_seconds=(
                row["interval_seconds"]
                if "interval_seconds" in row.keys()
                else None
            ),
            cron_expression=(
                str(row["cron_expression"] or "")
                if "cron_expression" in row.keys()
                else ""
            ),
            last_run_at=row["last_run_at"] if "last_run_at" in row.keys() else None,
        )


@dataclass
class DelegatedTaskRecord:
    run_id: str
    conversation_id: str
    prompt: str
    workspace: str
    report_token: str
    status: str
    report: str = ""
    report_event_id: str = ""
    exit_code: int | None = None
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "DelegatedTaskRecord":
        keys = set(row.keys())
        return cls(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            prompt=row["prompt"],
            workspace=row["workspace"],
            report_token=row["report_token"],
            status=row["status"],
            report=row["report"] if "report" in keys else "",
            report_event_id=row["report_event_id"] if "report_event_id" in keys else "",
            exit_code=row["exit_code"] if "exit_code" in keys else None,
            created_at=row["created_at"] if "created_at" in keys else None,
            started_at=row["started_at"] if "started_at" in keys else None,
            finished_at=row["finished_at"] if "finished_at" in keys else None,
        )


class MessageStore:
    """SQLite 持久化消息存储。"""

    _DDL = """
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            group_key   TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            created_at  REAL    NOT NULL DEFAULT (julianday('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_messages_date
            ON messages(date);
        CREATE INDEX IF NOT EXISTS idx_messages_group
            ON messages(group_key);
        CREATE INDEX IF NOT EXISTS idx_messages_category
            ON messages(category);

        CREATE TABLE IF NOT EXISTS summaries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key   TEXT    NOT NULL,
            seq         INTEGER NOT NULL,
            source      TEXT    NOT NULL,
            summary     TEXT    NOT NULL,
            last_message_id INTEGER DEFAULT 0,
            kind        TEXT    NOT NULL DEFAULT 'message',
            covered_through_message_id INTEGER,
            created_at  REAL    NOT NULL DEFAULT (julianday('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_group
            ON summaries(group_key, seq);

        CREATE TABLE IF NOT EXISTS actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key       TEXT    NOT NULL,
            tool_name       TEXT    NOT NULL,
            call_id         TEXT    NOT NULL DEFAULT '',
            success         INTEGER NOT NULL,
            arguments_json  TEXT    NOT NULL,
            result          TEXT    NOT NULL,
            artifact_json   TEXT,
            created_at      REAL    NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_actions_group_time
            ON actions(group_key, id);

        CREATE TABLE IF NOT EXISTS events (
            sequence         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         TEXT NOT NULL UNIQUE,
            conversation_id  TEXT NOT NULL,
            actor_id         TEXT NOT NULL,
            actor_kind       TEXT NOT NULL,
            actor_name       TEXT NOT NULL DEFAULT '',
            event_type       TEXT NOT NULL,
            content          TEXT NOT NULL DEFAULT '',
            media_ids_json   TEXT NOT NULL DEFAULT '[]',
            payload_json     TEXT NOT NULL DEFAULT '{}',
            turn_id          TEXT NOT NULL DEFAULT '',
            pipeline_id      TEXT NOT NULL DEFAULT '',
            visibility       TEXT NOT NULL DEFAULT 'private',
            audience         TEXT NOT NULL DEFAULT 'bot:self',
            status           TEXT NOT NULL DEFAULT 'finalized',
            created_at       REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_conversation
            ON events(conversation_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_events_actor
            ON events(actor_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_events_status
            ON events(status, sequence);
        CREATE INDEX IF NOT EXISTS idx_events_activity
            ON events(event_type, created_at, conversation_id);

        CREATE TABLE IF NOT EXISTS actors (
            actor_id             TEXT PRIMARY KEY,
            kind                 TEXT NOT NULL,
            platform             TEXT NOT NULL DEFAULT '',
            platform_subject_id  TEXT NOT NULL DEFAULT '',
            display_name         TEXT NOT NULL DEFAULT '',
            private_alias        TEXT NOT NULL DEFAULT '',
            relationship         TEXT NOT NULL DEFAULT '',
            metadata_json        TEXT NOT NULL DEFAULT '{}',
            created_at           REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            updated_at           REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_actors_kind ON actors(kind);

        CREATE TABLE IF NOT EXISTS episodes (
            episode_id       TEXT PRIMARY KEY,
            conversation_id  TEXT NOT NULL,
            first_sequence   INTEGER NOT NULL,
            last_sequence    INTEGER NOT NULL,
            participants_json TEXT NOT NULL DEFAULT '[]',
            narrative        TEXT NOT NULL,
            open_loops       TEXT NOT NULL DEFAULT '',
            media_ids_json   TEXT NOT NULL DEFAULT '[]',
            status           TEXT NOT NULL DEFAULT 'ready',
            created_at       REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            UNIQUE(conversation_id, first_sequence, last_sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_conversation
            ON episodes(conversation_id, first_sequence);

        CREATE TABLE IF NOT EXISTS media_ledger (
            media_id         TEXT PRIMARY KEY,
            sha256           TEXT NOT NULL UNIQUE,
            kind             TEXT NOT NULL,
            path             TEXT NOT NULL DEFAULT '',
            source_url       TEXT NOT NULL DEFAULT '',
            mime_type        TEXT NOT NULL DEFAULT '',
            description      TEXT NOT NULL DEFAULT '',
            short_description TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'active',
            created_at       REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            last_used_at     REAL
        );

        CREATE INDEX IF NOT EXISTS idx_media_kind_status
            ON media_ledger(kind, status);

        CREATE TABLE IF NOT EXISTS canonical_state (
            state_key        TEXT PRIMARY KEY,
            content          TEXT NOT NULL,
            source_event_ids TEXT NOT NULL DEFAULT '[]',
            version          INTEGER NOT NULL DEFAULT 1,
            updated_at       REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS inner_journal (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id           TEXT NOT NULL DEFAULT '',
            source_conversation_id TEXT NOT NULL DEFAULT '',
            source_actor_id       TEXT NOT NULL DEFAULT '',
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            content               TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'finalized',
            created_at            REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_inner_journal_created
            ON inner_journal(created_at DESC, id DESC);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content, group_key, category,
            content=messages
        );

        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, group_key, category)
            VALUES (new.rowid, new.content, new.group_key, new.category);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, group_key, category)
            VALUES ('delete', old.rowid, old.content, old.group_key, old.category);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, group_key, category)
            VALUES ('delete', old.rowid, old.content, old.group_key, old.category);
            INSERT INTO messages_fts(rowid, content, group_key, category)
            VALUES (new.rowid, new.content, new.group_key, new.category);
        END;

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key       TEXT    NOT NULL,
            peer_chat_type  INTEGER NOT NULL,
            peer_uid        TEXT    NOT NULL,
            prompt          TEXT    NOT NULL,
            context         TEXT    NOT NULL DEFAULT '',
            scheduled_at    REAL    NOT NULL,
            schedule_kind   TEXT    NOT NULL DEFAULT 'once',
            interval_seconds REAL,
            cron_expression TEXT    NOT NULL DEFAULT '',
            last_run_at     REAL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            created_at      REAL    NOT NULL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_time
            ON scheduled_tasks(status, scheduled_at);

        CREATE TABLE IF NOT EXISTS delegated_tasks (
            run_id          TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            workspace       TEXT NOT NULL,
            report_token    TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            report          TEXT NOT NULL DEFAULT '',
            report_event_id TEXT NOT NULL DEFAULT '',
            exit_code       INTEGER,
            created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            started_at      REAL,
            finished_at     REAL
        );

        CREATE INDEX IF NOT EXISTS idx_delegated_tasks_status
            ON delegated_tasks(status, created_at);
    """

    def __init__(self, db_path: str = "data/mutsumi.db", media_dir: str = "data/media"):
        self.db_path = Path(db_path)
        self.media_dir = Path(media_dir)
        self._conn: aiosqlite.Connection | None = None
        self._episode_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(self._DDL)

        try:
            await self._conn.execute(
                "ALTER TABLE summaries ADD COLUMN last_message_id INTEGER DEFAULT 0"
            )
        except Exception:
            pass
        for statement in (
            "ALTER TABLE summaries ADD COLUMN kind TEXT NOT NULL DEFAULT 'message'",
            "ALTER TABLE summaries ADD COLUMN covered_through_message_id INTEGER",
            "ALTER TABLE events ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE scheduled_tasks ADD COLUMN schedule_kind TEXT NOT NULL DEFAULT 'once'",
            "ALTER TABLE scheduled_tasks ADD COLUMN interval_seconds REAL",
            "ALTER TABLE scheduled_tasks ADD COLUMN cron_expression TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE scheduled_tasks ADD COLUMN last_run_at REAL",
            "ALTER TABLE scheduled_tasks ADD COLUMN context TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE delegated_tasks ADD COLUMN report TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE delegated_tasks ADD COLUMN report_event_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE delegated_tasks ADD COLUMN exit_code INTEGER",
            "ALTER TABLE delegated_tasks ADD COLUMN created_at REAL NOT NULL DEFAULT 0",
            "ALTER TABLE delegated_tasks ADD COLUMN started_at REAL",
            "ALTER TABLE delegated_tasks ADD COLUMN finished_at REAL",
        ):
            try:
                await self._conn.execute(statement)
            except Exception:
                pass

        await self._conn.commit()
        logger.info("MessageStore initialized at %s", self.db_path)

    async def save(self, msg: StoredMessage) -> int:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "INSERT INTO messages (date, group_key, category, content) VALUES (?, ?, ?, ?)",
            (msg.date, msg.group_key, msg.category, msg.content),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def append_event(self, event: EventRecord) -> EventRecord:
        """Append one immutable fact and return it with its ledger sequence."""
        self._ensure_initialized()
        values = event.to_insert_values()
        cursor = await self._conn.execute(
            "INSERT INTO events (event_id, conversation_id, actor_id, actor_kind, "
            "actor_name, event_type, content, media_ids_json, payload_json, turn_id, pipeline_id, "
            "visibility, audience, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        await self._conn.commit()
        row_cursor = await self._conn.execute(
            "SELECT * FROM events WHERE sequence = ?", (cursor.lastrowid,)
        )
        row = await row_cursor.fetchone()
        if row is None:
            raise RuntimeError("event insert succeeded but the row cannot be read")
        return EventRecord.from_row(row)

    async def get_event(self, event_id: str) -> EventRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        )
        row = await cursor.fetchone()
        return EventRecord.from_row(row) if row else None

    async def get_pending_service_events(self) -> list[EventRecord]:
        """Return accepted external events that were not finalized before shutdown."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM events WHERE event_type = 'inbound' AND actor_kind = 'service' "
            "AND status = 'received' ORDER BY sequence ASC",
        )
        return [EventRecord.from_row(row) for row in await cursor.fetchall()]

    async def ensure_actor(self, actor: ActorRecord) -> ActorRecord:
        self._ensure_initialized()
        await self._conn.execute(
            "INSERT INTO actors (actor_id, kind, platform, platform_subject_id, display_name, "
            "private_alias, relationship, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(actor_id) DO UPDATE SET kind=excluded.kind, platform=excluded.platform, "
            "platform_subject_id=excluded.platform_subject_id, display_name=excluded.display_name, "
            "updated_at=strftime('%s', 'now')",
            (
                actor.actor_id,
                actor.kind,
                actor.platform,
                actor.platform_subject_id,
                actor.display_name,
                actor.private_alias,
                actor.relationship,
                json.dumps(actor.metadata or {}, ensure_ascii=False),
            ),
        )
        await self._conn.commit()
        return await self.get_actor(actor.actor_id) or actor

    async def get_actor(self, actor_id: str) -> ActorRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,))
        row = await cursor.fetchone()
        return ActorRecord.from_row(row) if row else None

    async def list_actors(self, limit: int = 500) -> list[ActorRecord]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM actors ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),)
        )
        return [ActorRecord.from_row(row) for row in await cursor.fetchall()]

    async def update_actor_profile(
        self,
        actor_id: str,
        *,
        private_alias: str | None = None,
        relationship: str | None = None,
    ) -> ActorRecord | None:
        self._ensure_initialized()
        fields: list[str] = []
        params: list[str] = []
        if private_alias is not None:
            fields.append("private_alias = ?")
            params.append(private_alias.strip())
        if relationship is not None:
            fields.append("relationship = ?")
            params.append(relationship.strip())
        if not fields:
            return await self.get_actor(actor_id)
        fields.append("updated_at = strftime('%s', 'now')")
        params.append(actor_id)
        await self._conn.execute(
            f"UPDATE actors SET {', '.join(fields)} WHERE actor_id = ?", params
        )
        await self._conn.commit()
        return await self.get_actor(actor_id)

    async def update_event_status(self, event_id: str, status: str, content: str | None = None) -> None:
        """Update lifecycle state without changing an event's identity."""
        self._ensure_initialized()
        if content is None:
            await self._conn.execute(
                "UPDATE events SET status = ? WHERE event_id = ?", (status, event_id)
            )
        else:
            await self._conn.execute(
                "UPDATE events SET status = ?, content = ? WHERE event_id = ?",
                (status, content, event_id),
            )
        await self._conn.commit()

    async def update_event_record(
        self,
        event_id: str,
        *,
        status: str | None = None,
        content: str | None = None,
        payload: dict[str, Any] | None = None,
        media_ids: list[str] | None = None,
    ) -> None:
        """Update mutable lifecycle fields of a still-open ledger event.

        Event identity and sequence stay immutable. This is used for staged
        tool results: the provider receives a provisional result immediately,
        then cleanup finalizes that same ledger row instead of appending a
        second result for the same tool call.
        """
        self._ensure_initialized()
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if content is not None:
            fields.append("content = ?")
            params.append(content)
        if payload is not None:
            fields.append("payload_json = ?")
            params.append(json.dumps(payload, ensure_ascii=False))
        if media_ids is not None:
            fields.append("media_ids_json = ?")
            params.append(json.dumps(media_ids, ensure_ascii=False))
        if not fields:
            return
        params.append(event_id)
        await self._conn.execute(
            f"UPDATE events SET {', '.join(fields)} WHERE event_id = ?",
            params,
        )
        await self._conn.commit()

    async def update_pipeline_status(self, pipeline_id: str, status: str) -> None:
        """Close unfinished pipeline records while preserving sent output facts."""
        self._ensure_initialized()
        if not str(pipeline_id).strip():
            return
        await self._conn.execute(
            "UPDATE events SET status = ? WHERE pipeline_id = ? "
            "AND event_type != 'outbound'",
            (status, pipeline_id),
        )
        await self._conn.commit()

    async def cancel_orphaned_pipelines(self) -> int:
        """Close non-service pipeline fragments left by a hard process restart.

        A service event is intentionally excluded: ingress and OpenCode report
        events have their own durable restart queues. Ordinary user, schedule,
        and heartbeat fragments cannot still be running when a new scheduler
        instance starts, so leaving them as ``received`` would block Episode
        reclamation forever while they remain correctly hidden from history.
        """
        self._ensure_initialized()
        terminal = tuple(NON_PROJECTABLE_EVENT_STATUSES | {"finalized"})
        placeholders = ",".join("?" for _ in terminal)
        cursor = await self._conn.execute(
            "SELECT pipeline_id FROM events "
            "WHERE pipeline_id != '' AND status NOT IN (" + placeholders + ") "
            "GROUP BY pipeline_id "
            "HAVING SUM(CASE WHEN actor_kind = 'service' THEN 1 ELSE 0 END) = 0",
            terminal,
        )
        pipeline_ids = [str(row["pipeline_id"]) for row in await cursor.fetchall()]
        for pipeline_id in pipeline_ids:
            await self._conn.execute(
                "UPDATE events SET status = 'cancelled' "
                "WHERE pipeline_id = ? AND event_type != 'outbound' "
                "AND status NOT IN (" + placeholders + ")",
                (pipeline_id, *terminal),
            )
        if pipeline_ids:
            await self._conn.commit()
        return len(pipeline_ids)

    async def update_event_pipeline_id(self, event_id: str, pipeline_id: str) -> None:
        """Move a retryable accepted event to a fresh pipeline attempt."""
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE events SET pipeline_id = ? WHERE event_id = ?",
            (pipeline_id, event_id),
        )
        await self._conn.commit()

    async def update_event_media_ids(self, event_id: str, media_ids: list[str]) -> None:
        """Attach verified media ledger references to an existing event."""
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE events SET media_ids_json = ? WHERE event_id = ?",
            (json.dumps(media_ids, ensure_ascii=False), event_id),
        )
        await self._conn.commit()

    async def get_events(
        self,
        *,
        conversation_id: str | None = None,
        exclude_conversation_id: str | None = None,
        visibility: str | None = None,
        after_sequence: int = 0,
        max_sequence: int | None = None,
        limit: int = 200,
        finalized_only: bool = True,
        latest: bool = False,
    ) -> list[EventRecord]:
        self._ensure_initialized()
        query = "SELECT * FROM events WHERE sequence > ?"
        params: list[Any] = [after_sequence]
        if conversation_id is not None:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        if exclude_conversation_id is not None:
            query += " AND conversation_id != ?"
            params.append(exclude_conversation_id)
        if visibility is not None:
            query += " AND visibility = ?"
            params.append(visibility)
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if finalized_only:
            query += " AND status = 'finalized'"
        query += f" ORDER BY sequence {'DESC' if latest else 'ASC'} LIMIT ?"
        params.append(limit)
        cursor = await self._conn.execute(query, params)
        rows = list(await cursor.fetchall())
        if latest:
            rows.reverse()
        return [EventRecord.from_row(row) for row in rows]

    async def get_events_outside_episode_ranges(
        self,
        episodes: list[EpisodeRecord],
        *,
        max_sequence: int | None = None,
        limit: int = 200,
        finalized_only: bool = False,
        latest: bool = False,
        audience_conversation_id: str | None = None,
    ) -> list[EventRecord]:
        """Return raw events not covered by the supplied verified Episodes.

        The caller owns Episode validation. Keeping the exclusion ranges in
        this query prevents a recent-event limit from filling up with rows
        that have already been replaced by an Episode, while still retaining
        raw events in older uncovered gaps.
        """
        self._ensure_initialized()
        query = "SELECT * FROM events WHERE 1 = 1"
        params: list[Any] = []
        if finalized_only:
            query += " AND status = 'finalized'"
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if audience_conversation_id:
            query += " AND (visibility = 'global' OR conversation_id = ?)"
            params.append(audience_conversation_id)
        for episode in episodes:
            query += (
                " AND NOT (conversation_id = ? AND sequence BETWEEN ? AND ?)"
            )
            params.extend((
                episode.conversation_id,
                int(episode.first_sequence),
                int(episode.last_sequence),
            ))
        query += f" ORDER BY sequence {'DESC' if latest else 'ASC'} LIMIT ?"
        params.append(max(1, int(limit)))
        cursor = await self._conn.execute(query, params)
        rows = list(await cursor.fetchall())
        if latest:
            rows.reverse()
        return [EventRecord.from_row(row) for row in rows]

    async def has_events_outside_episode_ranges_before(
        self,
        episodes: list[EpisodeRecord],
        *,
        before_sequence: int,
        max_sequence: int | None = None,
        finalized_only: bool = False,
        audience_conversation_id: str | None = None,
    ) -> bool:
        """Detect an older raw event hidden by a bounded projection page."""
        self._ensure_initialized()
        query = (
            "SELECT 1 FROM events WHERE sequence < ? "
            "AND event_type IN ('inbound', 'outbound', 'tool_call', 'tool_result')"
        )
        params: list[Any] = [max(1, int(before_sequence))]
        if finalized_only:
            query += " AND status = 'finalized'"
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if audience_conversation_id:
            query += " AND (visibility = 'global' OR conversation_id = ?)"
            params.append(audience_conversation_id)
        for episode in episodes:
            query += (
                " AND NOT (conversation_id = ? AND sequence BETWEEN ? AND ?)"
            )
            params.extend((
                episode.conversation_id,
                int(episode.first_sequence),
                int(episode.last_sequence),
            ))
        query += " LIMIT 1"
        cursor = await self._conn.execute(query, params)
        return await cursor.fetchone() is not None

    async def get_legacy_events_outside_episode_ranges(
        self,
        episodes: list[EpisodeRecord],
        *,
        max_sequence: int | None = None,
        audience_conversation_id: str | None = None,
    ) -> list[EventRecord]:
        """Load legacy Ledger rows that have no pipeline identity.

        Current pipeline writes always include ``pipeline_id``. Older rows
        cannot identify the rest of a turn from one paged event, so the
        compatibility path reads them as a complete set and lets the normal
        logical-group/token projection apply the final bound.
        """
        self._ensure_initialized()
        query = "SELECT * FROM events WHERE pipeline_id = ''"
        params: list[Any] = []
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if audience_conversation_id:
            query += " AND (visibility = 'global' OR conversation_id = ?)"
            params.append(audience_conversation_id)
        for episode in episodes:
            query += (
                " AND NOT (conversation_id = ? AND sequence BETWEEN ? AND ?)"
            )
            params.extend((
                episode.conversation_id,
                int(episode.first_sequence),
                int(episode.last_sequence),
            ))
        query += " ORDER BY sequence ASC"
        cursor = await self._conn.execute(query, params)
        return [EventRecord.from_row(row) for row in await cursor.fetchall()]

    async def get_events_by_pipeline_ids(
        self,
        pipeline_ids: set[str] | list[str],
        *,
        max_sequence: int | None = None,
        finalized_only: bool = False,
        audience_conversation_id: str | None = None,
    ) -> list[EventRecord]:
        """Load complete pipeline groups needed by a bounded projection."""
        self._ensure_initialized()
        values = [str(value) for value in pipeline_ids if str(value)]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        query = (
            "SELECT * FROM events WHERE pipeline_id IN ("
            + placeholders
            + ")"
        )
        params: list[Any] = values
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if finalized_only:
            query += " AND status = 'finalized'"
        if audience_conversation_id:
            query += " AND (visibility = 'global' OR conversation_id = ?)"
            params.append(audience_conversation_id)
        query += " ORDER BY sequence ASC"
        cursor = await self._conn.execute(query, params)
        return [EventRecord.from_row(row) for row in await cursor.fetchall()]

    async def get_events_by_turn_ids(
        self,
        turn_ids: set[str] | list[str],
        *,
        max_sequence: int | None = None,
        finalized_only: bool = False,
        audience_conversation_id: str | None = None,
    ) -> list[EventRecord]:
        """Load complete legacy tool rounds that have no pipeline id."""
        self._ensure_initialized()
        values = [str(value) for value in turn_ids if str(value)]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        query = "SELECT * FROM events WHERE turn_id IN (" + placeholders + ")"
        params: list[Any] = values
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if finalized_only:
            query += " AND status = 'finalized'"
        if audience_conversation_id:
            query += " AND (visibility = 'global' OR conversation_id = ?)"
            params.append(audience_conversation_id)
        query += " ORDER BY sequence ASC"
        cursor = await self._conn.execute(query, params)
        return [EventRecord.from_row(row) for row in await cursor.fetchall()]

    async def get_events_by_sequence(
        self,
        first_sequence: int,
        last_sequence: int,
        *,
        conversation_id: str | None = None,
        max_sequence: int | None = None,
        finalized_only: bool = False,
    ) -> list[EventRecord]:
        self._ensure_initialized()
        query = "SELECT * FROM events WHERE sequence BETWEEN ? AND ?"
        params: list[Any] = [first_sequence, last_sequence]
        if conversation_id is not None:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        if max_sequence is not None:
            query += " AND sequence <= ?"
            params.append(max(0, int(max_sequence)))
        if finalized_only:
            query += " AND status = 'finalized'"
        query += " ORDER BY sequence ASC"
        cursor = await self._conn.execute(query, params)
        return [EventRecord.from_row(row) for row in await cursor.fetchall()]

    async def get_event_high_water_sequence(self) -> int:
        """Return the ledger position visible to a new context snapshot."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events"
        )
        row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def get_recent_active_conversations(
        self,
        *,
        since: float,
        chat_type: str,
    ) -> list[dict[str, Any]]:
        """Return conversations with real recent inbound activity."""
        self._ensure_initialized()
        prefix = "private:" if chat_type == "private" else "group:"
        cursor = await self._conn.execute(
            "SELECT conversation_id, actor_id, actor_name, MAX(created_at) AS last_active "
            "FROM events WHERE event_type = 'inbound' AND status != 'cancelled' "
            "AND created_at >= ? AND conversation_id LIKE ? "
            "GROUP BY conversation_id ORDER BY last_active ASC",
            (since, f"{prefix}%"),
        )
        return [
            {
                "conversation_id": row["conversation_id"],
                "actor_id": row["actor_id"],
                "actor_name": row["actor_name"],
                "last_active": row["last_active"],
            }
            for row in await cursor.fetchall()
        ]

    async def has_unanswered_proactive(self, conversation_id: str) -> bool:
        """Return whether a heartbeat output has no later real inbound reply."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE conversation_id = ? "
            "AND event_type = 'outbound' AND status = 'finalized' "
            "AND pipeline_id LIKE 'heartbeat:%'",
            (conversation_id,),
        )
        proactive_row = await cursor.fetchone()
        proactive_sequence = int(proactive_row[0] or 0)
        if not proactive_sequence:
            return False
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE conversation_id = ? "
            "AND event_type = 'inbound' AND status != 'cancelled'",
            (conversation_id,),
        )
        inbound_row = await cursor.fetchone()
        return proactive_sequence > int(inbound_row[0] or 0)

    async def add_episode(self, episode: EpisodeRecord) -> str:
        self._ensure_initialized()
        episode_id = episode.episode_id or f"ep_{uuid.uuid4().hex}"
        if episode.first_sequence <= 0 or episode.last_sequence < episode.first_sequence:
            raise ValueError("episode coverage must be a positive ordered range")

        # The in-process lock prevents normal races. BEGIN IMMEDIATE makes the
        # exact-range de-duplication meaningful when two writers share a DB.
        # Semantic overlap is resolved by the canonical projector after it has
        # validated every Episode; storage cannot know whether an old ready row
        # cuts an incomplete tool round.
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                "SELECT episode_id, first_sequence, last_sequence FROM episodes "
                "WHERE conversation_id = ? AND status = 'ready' "
                "AND first_sequence = ? AND last_sequence = ? LIMIT 1",
                (episode.conversation_id, episode.first_sequence, episode.last_sequence),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await self._conn.rollback()
                return str(existing["episode_id"])

            await self._conn.execute(
                "INSERT INTO episodes (episode_id, conversation_id, first_sequence, last_sequence, "
                "participants_json, narrative, open_loops, media_ids_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, episode.conversation_id, episode.first_sequence, episode.last_sequence,
                 episode.participants_json, episode.narrative, episode.open_loops,
                 episode.media_ids_json, episode.status),
            )
            await self._conn.commit()
            return episode_id
        except Exception:
            await self._conn.rollback()
            raise

    def episode_lock(self, conversation_id: str) -> asyncio.Lock:
        """Serialize archive selection and commit for one conversation.

        Idle timers and token-pressure reclamation can reach the same store at
        the same time. The ledger remains the source of truth, but serializing
        the selection prevents two provider calls from summarizing the same
        uncovered prefix.
        """
        key = str(conversation_id)
        lock = self._episode_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._episode_locks[key] = lock
        return lock

    async def get_episodes(self, conversation_id: str, limit: int = 20) -> list[EpisodeRecord]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM episodes WHERE conversation_id = ? ORDER BY first_sequence DESC LIMIT ?",
            (conversation_id, limit),
        )
        rows = list(await cursor.fetchall())
        rows.reverse()
        return [EpisodeRecord.from_row(row) for row in rows]

    async def get_all_episodes(
        self,
        limit: int = 2000,
        *,
        max_sequence: int | None = None,
    ) -> list[EpisodeRecord]:
        """Return ready episode projections in global sequence order."""
        self._ensure_initialized()
        query = (
            "SELECT * FROM episodes WHERE status = 'ready'"
        )
        params: list[Any] = []
        if max_sequence is not None:
            query += " AND last_sequence <= ?"
            params.append(max(0, int(max_sequence)))
        query += " ORDER BY first_sequence ASC LIMIT ?"
        params.append(max(1, int(limit)))
        cursor = await self._conn.execute(query, params)
        return [EpisodeRecord.from_row(row) for row in await cursor.fetchall()]

    async def get_latest_episode_sequence(
        self,
        conversation_id: str,
        *,
        max_sequence: int | None = None,
    ) -> int:
        """Return the contiguous Episode frontier for one conversation.

        ``MAX(last_sequence)`` is not a valid coverage cursor: an older
        Episode can be missing between two ready rows. The frontier advances
        only while every non-terminal conversation event is inside the next
        Episode. Terminal cancelled/error rows are already excluded from
        model history and therefore do not block the frontier.
        """
        self._ensure_initialized()
        # Episode validation is deliberately owned by the projection module.
        # Reimplementing only endpoint/range checks here lets an old Episode
        # that cuts a tool round advance the archive cursor and hide raw facts
        # after a restart.  Import lazily because projection imports the store
        # data classes.
        from .projection import (
            _select_valid_episodes,
            frontier_relevant_event_sequences,
        )

        episodes = await self.get_episodes(conversation_id, limit=1_000_000)
        valid_episodes, covered_by_episode = await _select_valid_episodes(
            self,
            episodes,
            max_sequence=max_sequence,
            require_complete_groups=True,
        )
        if not valid_episodes:
            return 0

        conversation_events = await self.get_events(
            conversation_id=conversation_id,
            max_sequence=max_sequence,
            limit=1_000_000,
            finalized_only=False,
        )
        event_sequences = sorted(frontier_relevant_event_sequences(conversation_events))
        if not event_sequences:
            return 0

        frontier = 0
        index = 0
        for episode in sorted(valid_episodes, key=lambda item: (item.first_sequence, item.last_sequence)):
            first = int(episode.first_sequence)
            last = int(episode.last_sequence)
            covered = sorted(
                sequence
                for sequence in covered_by_episode.get(episode.episode_id or "", set())
                if sequence in event_sequences
            )
            if not covered:
                continue
            expected = event_sequences[index:index + len(covered)]
            if covered != expected or covered[0] != event_sequences[index]:
                break
            frontier = covered[-1]
            index += len(covered)
            if index >= len(event_sequences):
                break
        return frontier

    async def update_message_content(self, msg_id: int, content: str) -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (content, msg_id),
        )
        await self._conn.commit()

    async def save_media(self, group_key: str, category: str, data: bytes, ext: str = "") -> int:
        """保存二进制媒体文件并写入数据库记录。"""
        self._ensure_initialized()
        today = date.today().isoformat()
        ext = ext.lstrip(".")

        subdir = {MessageCategory.IMAGE: "images", MessageCategory.AUDIO: "audio", MessageCategory.VIDEO: "video"}.get(
            category, "other"  # type: ignore[arg-type]
        )
        dest_dir = self.media_dir / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)

        kind = category.value if isinstance(category, MessageCategory) else str(category)
        record = await self.register_media(data, kind=kind, ext=ext)
        filepath = Path(record.path)
        media_id = record.media_id
        digest = record.sha256

        content = json.dumps({"media_id": media_id, "file": str(filepath), "sha256": digest, "size": len(data)})
        return await self.save(StoredMessage(
            date=today, group_key=group_key, category=category, content=content,
        ))

    async def register_media(
        self,
        data: bytes,
        *,
        kind: str,
        ext: str = "",
        source_url: str = "",
    ) -> MediaRecord:
        """Register binary media once, returning its stable ledger identity."""
        self._ensure_initialized()
        ext = ext.lstrip(".")
        digest = hashlib.sha256(data).hexdigest()
        media_id = f"media_{digest[:24]}"
        existing = await self.get_media(media_id)
        if existing is not None:
            await self._conn.execute(
                "UPDATE media_ledger SET last_used_at = strftime('%s', 'now') WHERE media_id = ?",
                (media_id,),
            )
            await self._conn.commit()
            return existing

        subdir = {"image": "images", "audio": "audio", "video": "video"}.get(kind, "other")
        dest_dir = self.media_dir / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        filepath = dest_dir / f"{digest}{'.' + ext if ext else ''}"
        filepath.write_bytes(data)
        await self._conn.execute(
            "INSERT INTO media_ledger (media_id, sha256, kind, path, source_url, mime_type, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'))",
            (media_id, digest, kind, str(filepath), source_url, _mime_for_extension(ext)),
        )
        await self._conn.commit()
        result = await self.get_media(media_id)
        if result is None:
            raise RuntimeError("media insert succeeded but the row cannot be read")
        return result

    async def register_external_media(self, source_url: str, *, kind: str = "image") -> MediaRecord:
        """Track a remote media reference without downloading it."""
        self._ensure_initialized()
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        media_id = f"media_url_{digest[:24]}"
        existing = await self.get_media(media_id)
        if existing is not None:
            return existing
        await self._conn.execute(
            "INSERT INTO media_ledger (media_id, sha256, kind, source_url, mime_type, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))",
            (media_id, digest, kind, source_url, _mime_for_extension(source_url.rsplit('.', 1)[-1])),
        )
        await self._conn.commit()
        result = await self.get_media(media_id)
        if result is None:
            raise RuntimeError("external media insert succeeded but the row cannot be read")
        return result

    async def get_media(self, media_id: str) -> MediaRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM media_ledger WHERE media_id = ?", (media_id,)
        )
        row = await cursor.fetchone()
        return MediaRecord.from_row(row) if row else None

    async def get_media_by_sha256(self, sha256: str) -> MediaRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM media_ledger WHERE sha256 = ?", (sha256,)
        )
        row = await cursor.fetchone()
        return MediaRecord.from_row(row) if row else None

    async def list_media(self, *, kind: str | None = None, status: str = "active", limit: int = 100) -> list[MediaRecord]:
        self._ensure_initialized()
        query = "SELECT * FROM media_ledger WHERE status = ?"
        params: list[Any] = [status]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY last_used_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._conn.execute(query, params)
        return [MediaRecord.from_row(row) for row in await cursor.fetchall()]

    async def update_media_description(self, media_id: str, description: str, short_description: str = "") -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE media_ledger SET description = ?, short_description = ?, last_used_at = strftime('%s', 'now') WHERE media_id = ?",
            (description, short_description or description[:80], media_id),
        )
        await self._conn.commit()

    async def set_media_status(self, media_id: str, status: str) -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE media_ledger SET status = ? WHERE media_id = ?", (status, media_id)
        )
        await self._conn.commit()

    async def mark_media_used(self, media_id: str) -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE media_ledger SET last_used_at = strftime('%s', 'now') WHERE media_id = ?",
            (media_id,),
        )
        await self._conn.commit()

    async def get_canonical_state(self, state_key: str = "bot:self") -> dict | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT state_key, content, source_event_ids, version, updated_at "
            "FROM canonical_state WHERE state_key = ?", (state_key,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "state_key": row["state_key"],
            "content": row["content"],
            "source_event_ids": json.loads(row["source_event_ids"] or "[]"),
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    async def upsert_canonical_state(
        self,
        content: str,
        *,
        state_key: str = "bot:self",
        source_event_ids: list[str] | None = None,
    ) -> int:
        self._ensure_initialized()
        existing = await self.get_canonical_state(state_key)
        version = int(existing["version"] if existing else 0) + 1
        await self._conn.execute(
            "INSERT INTO canonical_state (state_key, content, source_event_ids, version) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(state_key) DO UPDATE SET "
            "content=excluded.content, source_event_ids=excluded.source_event_ids, "
            "version=excluded.version, updated_at=strftime('%s', 'now')",
            (state_key, content, json.dumps(source_event_ids or [], ensure_ascii=False), version),
        )
        await self._conn.commit()
        return version

    async def clear_canonical_state(self, state_key: str = "bot:self") -> None:
        self._ensure_initialized()
        await self._conn.execute("DELETE FROM canonical_state WHERE state_key = ?", (state_key,))
        await self._conn.commit()

    async def get_messages(
        self,
        group_key: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StoredMessage]:
        self._ensure_initialized()

        query = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []

        if group_key is not None:
            query += " AND group_key = ?"
            params.append(group_key)
        if date_from is not None:
            query += " AND date >= ?"
            params.append(date_from)
        if date_to is not None:
            query += " AND date <= ?"
            params.append(date_to)
        if category is not None:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [StoredMessage.from_row(r) for r in rows]

    async def get_context_for_group(
        self,
        group_key: str,
        limit: int = 50,
    ) -> list[StoredMessage]:
        """获取指定消息组的最近消息，用于上下文拼接。"""
        return await self.get_messages(group_key=group_key, limit=limit)

    async def count(self, group_key: str | None = None) -> int:
        self._ensure_initialized()
        if group_key:
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE group_key = ?", (group_key,)
            )
        else:
            cursor = await self._conn.execute("SELECT COUNT(*) FROM messages")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def add_summary(
        self,
        group_key: str,
        source: str,
        summary: str,
        last_message_id: int = 0,
        *,
        kind: str = "message",
        covered_through_message_id: int | None = None,
    ) -> int:
        self._ensure_initialized()
        if kind != "compaction":
            # A per-message archive note is descriptive only. It must never
            # advance the restart/recovery cursor and hide unrelated records.
            covered_through_message_id = None
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM summaries WHERE group_key = ?",
            (group_key,)
        )
        row = await cursor.fetchone()
        next_seq = row[0] if row else 1

        cursor = await self._conn.execute(
            "INSERT INTO summaries "
            "(group_key, seq, source, summary, last_message_id, kind, covered_through_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_key, next_seq, source, summary, last_message_id, kind, covered_through_message_id),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_summaries(self, group_key: str, limit: int = 180) -> list[dict]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, group_key, seq, source, summary, last_message_id, kind, "
            "covered_through_message_id, created_at FROM summaries "
            "WHERE group_key = ? ORDER BY seq ASC LIMIT ?",
            (group_key, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "seq": r["seq"],
                "source": r["source"],
                "summary": r["summary"],
                "last_message_id": r["last_message_id"],
                "kind": r["kind"],
                "covered_through_message_id": r["covered_through_message_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def trim_summaries(self, group_key: str, max_count: int = 180, min_count: int = 90) -> int:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM summaries WHERE group_key = ?", (group_key,)
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        if count <= max_count:
            return 0

        target_to_keep = min_count
        to_delete = count - target_to_keep
        cursor = await self._conn.execute(
            "DELETE FROM summaries WHERE id IN (SELECT id FROM summaries WHERE group_key = ? ORDER BY seq ASC LIMIT ?)",
            (group_key, to_delete),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def get_current_self_note(self, group_key: str) -> dict | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, content, created_at FROM messages WHERE group_key = ? AND category = 'self_note' ORDER BY id DESC LIMIT 1",
            (group_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "content": row["content"], "created_at": row["created_at"]}

    async def upsert_self_note(self, group_key: str, content: str) -> None:
        self._ensure_initialized()
        today = date.today().isoformat()
        await self._conn.execute(
            "INSERT INTO messages (date, group_key, category, content) VALUES (?, ?, 'self_note', ?)",
            (today, group_key, content),
        )
        await self._conn.commit()

    async def get_current_priority_override(self, group_key: str) -> dict | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, content, created_at FROM messages WHERE group_key = ? AND category = 'priority_override' ORDER BY id DESC LIMIT 1",
            (group_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "content": row["content"], "created_at": row["created_at"]}

    async def append_inner_journal(
        self,
        *,
        content: str,
        pipeline_id: str = "",
        source_conversation_id: str = "",
        source_actor_id: str = "",
        source_event_ids: list[str] | None = None,
        status: str = "finalized",
    ) -> int | None:
        """Append a subjective bot-state entry unless it duplicates the latest entry."""
        self._ensure_initialized()
        normalized = " ".join(content.split())
        if not normalized:
            return None

        cursor = await self._conn.execute(
            "SELECT id, content FROM inner_journal ORDER BY id DESC LIMIT 1"
        )
        latest = await cursor.fetchone()
        if latest is not None and " ".join(str(latest["content"]).split()) == normalized:
            return None

        cursor = await self._conn.execute(
            "INSERT INTO inner_journal "
            "(pipeline_id, source_conversation_id, source_actor_id, source_event_ids_json, content, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                pipeline_id,
                source_conversation_id,
                source_actor_id,
                json.dumps(source_event_ids or [], ensure_ascii=False),
                content,
                status,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_inner_journal(self, limit: int = 200) -> list[dict[str, Any]]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, pipeline_id, source_conversation_id, source_actor_id, "
            "source_event_ids_json, content, status, created_at "
            "FROM inner_journal WHERE status = 'finalized' "
            "ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "pipeline_id": row["pipeline_id"],
                "source_conversation_id": row["source_conversation_id"],
                "source_actor_id": row["source_actor_id"],
                "source_event_ids": json.loads(row["source_event_ids_json"] or "[]"),
                "content": row["content"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    async def upsert_priority_override(self, group_key: str, content: str) -> None:
        self._ensure_initialized()
        today = date.today().isoformat()
        await self._conn.execute(
            "INSERT INTO messages (date, group_key, category, content) VALUES (?, ?, 'priority_override', ?)",
            (today, group_key, content),
        )
        await self._conn.commit()

    async def search_memory(self, group_key: str, query: str, limit: int = 5) -> list[dict]:
        self._ensure_initialized()
        try:
            cursor = await self._conn.execute(
                "SELECT m.id, m.date, m.group_key, m.category, m.content, m.created_at "
                "FROM messages_fts f JOIN messages m ON f.rowid = m.rowid "
                "WHERE f.content MATCH ? AND m.group_key = ? "
                "ORDER BY rank LIMIT ?",
                (query, group_key, limit),
            )
        except Exception:
            cursor = await self._conn.execute(
                "SELECT id, date, group_key, category, content, created_at FROM messages "
                "WHERE group_key = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (group_key, f"%{query}%", limit),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "date": r["date"],
                "category": r["category"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def get_messages_by_ids(self, ids: list[int]) -> list[dict]:
        self._ensure_initialized()
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        cursor = await self._conn.execute(
            f"SELECT id, date, group_key, category, content, created_at FROM messages WHERE id IN ({placeholders}) ORDER BY id ASC",
            ids,
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "date": r["date"],
                "category": r["category"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self._episode_locks.clear()
        logger.info("MessageStore closed")

    async def get_message_group_keys(self) -> list[str]:
        """Return all distinct group_keys from messages table."""
        self._ensure_initialized()
        cursor = await self._conn.execute("SELECT DISTINCT group_key FROM messages")
        rows = await cursor.fetchall()
        return [r["group_key"] for r in rows]

    async def get_event_conversation_ids(self) -> list[str]:
        """Return ledger conversations in their first-seen order."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT conversation_id, MIN(sequence) AS first_sequence "
            "FROM events GROUP BY conversation_id ORDER BY first_sequence ASC"
        )
        return [str(row["conversation_id"]) for row in await cursor.fetchall()]

    async def get_newest_compaction_summary(self, group_key: str) -> dict | None:
        """Return the newest summary with an explicitly trusted coverage boundary."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, seq, source, summary, kind, covered_through_message_id FROM summaries "
            "WHERE group_key = ? AND kind = 'compaction' "
            "AND covered_through_message_id IS NOT NULL "
            "ORDER BY covered_through_message_id DESC LIMIT 1",
            (group_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "seq": row["seq"],
            "source": row["source"],
            "summary": row["summary"],
            "kind": row["kind"],
            "covered_through_message_id": row["covered_through_message_id"],
        }

    async def get_latest_compaction_summary(self, group_key: str) -> dict | None:
        """Return the latest legacy compaction text, trusted or otherwise.

        A legacy window can be capped before its coverage is trustworthy. The
        summary is still useful inside the current process, but it must not be
        used as a restart cursor until it has a real message boundary.
        """
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, seq, source, summary, kind, covered_through_message_id, created_at "
            "FROM summaries WHERE group_key = ? AND kind = 'compaction' "
            "ORDER BY id DESC LIMIT 1",
            (group_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "seq": row["seq"],
            "source": row["source"],
            "summary": row["summary"],
            "kind": row["kind"],
            "covered_through_message_id": row["covered_through_message_id"],
            "created_at": row["created_at"],
        }

    async def get_restorable_messages(
        self,
        group_key: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        """Return newest successful conversation rows in chronological order."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, date, group_key, category, content, created_at FROM messages "
            "WHERE group_key = ? AND id > ? "
            "AND category IN ('short_text', 'long_text', 'image', 'text', 'mixed', 'proactive') "
            "ORDER BY id DESC",
            (group_key, after_id),
        )
        rows = await cursor.fetchall()
        selected: list[dict] = []
        for row in rows:
            try:
                parsed = json.loads(row["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict) or parsed.get("status") != "responded":
                continue
            if not str(parsed.get("user", "")).strip() and not str(parsed.get("bot", "")).strip():
                continue
            selected.append({
                "id": row["id"],
                "date": row["date"],
                "group_key": row["group_key"],
                "category": row["category"],
                "content": row["content"],
                "created_at": row["created_at"],
            })
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    async def save_action(
        self,
        *,
        group_key: str,
        tool_name: str,
        call_id: str,
        success: bool,
        arguments: dict,
        result: str,
        artifact: dict | None = None,
    ) -> int:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "INSERT INTO actions "
            "(group_key, tool_name, call_id, success, arguments_json, result, artifact_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                group_key,
                tool_name,
                call_id,
                1 if success else 0,
                json.dumps(arguments, ensure_ascii=False),
                result,
                json.dumps(artifact, ensure_ascii=False) if artifact is not None else None,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_recent_actions(self, group_key: str, limit: int = 12) -> list[dict]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, tool_name, call_id, success, arguments_json, result, artifact_json, created_at "
            "FROM actions WHERE group_key = ? ORDER BY id DESC LIMIT ?",
            (group_key, limit),
        )
        rows = list(await cursor.fetchall())
        rows.reverse()
        return [
            {
                "id": row["id"],
                "tool_name": row["tool_name"],
                "call_id": row["call_id"],
                "success": bool(row["success"]),
                "arguments": json.loads(row["arguments_json"]),
                "result": row["result"],
                "artifact": json.loads(row["artifact_json"]) if row["artifact_json"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def get_messages_after(self, group_key: str, after_id: int, limit: int = 200) -> list[dict]:
        """Return messages with id > after_id, ordered by id ASC."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, date, group_key, category, content, created_at FROM messages "
            "WHERE group_key = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (group_key, after_id, limit),
        )
        rows = await cursor.fetchall()
        return [{"id": r["id"], "date": r["date"], "category": r["category"], "content": r["content"],
                 "created_at": r["created_at"]}
                for r in rows]

    async def get_max_message_id(self, group_key: str) -> int:
        """Return the highest message id for a group, or 0 if none."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT MAX(id) FROM messages WHERE group_key = ?", (group_key,)
        )
        row = await cursor.fetchone()
        return row[0] or 0

    async def set_last_message_id(self, summary_id: int, last_message_id: int) -> None:
        """Set the coverage boundary for a summary row."""
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE summaries SET last_message_id = ? WHERE id = ?",
            (last_message_id, summary_id),
        )
        await self._conn.commit()

    async def add_scheduled_task(
        self,
        *,
        group_key: str,
        peer_chat_type: int,
        peer_uid: str,
        prompt: str,
        context: str = "",
        scheduled_at: float,
        schedule_kind: str = "once",
        interval_seconds: float | None = None,
        cron_expression: str = "",
    ) -> int:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "INSERT INTO scheduled_tasks "
            "(group_key, peer_chat_type, peer_uid, prompt, context, scheduled_at, "
            "schedule_kind, interval_seconds, cron_expression, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                group_key,
                peer_chat_type,
                peer_uid,
                prompt,
                context,
                scheduled_at,
                schedule_kind,
                interval_seconds,
                cron_expression,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_pending_scheduled_tasks(self) -> list[ScheduledTaskRecord]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, group_key, peer_chat_type, peer_uid, prompt, context, scheduled_at, "
            "schedule_kind, interval_seconds, cron_expression, last_run_at, status, created_at "
            "FROM scheduled_tasks WHERE status IN ('pending', 'running') ORDER BY scheduled_at ASC"
        )
        rows = await cursor.fetchall()
        return [ScheduledTaskRecord.from_row(row) for row in rows]

    async def get_scheduled_task(self, task_id: int) -> ScheduledTaskRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, group_key, peer_chat_type, peer_uid, prompt, context, scheduled_at, "
            "schedule_kind, interval_seconds, cron_expression, last_run_at, status, created_at "
            "FROM scheduled_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return ScheduledTaskRecord.from_row(row) if row else None

    async def list_scheduled_tasks(
        self,
        *,
        status: str | None = None,
        group_key: str | None = None,
        limit: int = 100,
    ) -> list[ScheduledTaskRecord]:
        self._ensure_initialized()
        query = (
            "SELECT id, group_key, peer_chat_type, peer_uid, prompt, context, scheduled_at, "
            "schedule_kind, interval_seconds, cron_expression, last_run_at, status, created_at "
            "FROM scheduled_tasks"
        )
        params: list[Any] = []
        filters: list[str] = []
        if status:
            filters.append("status = ?")
            params.append(status)
        if group_key:
            filters.append("group_key = ?")
            params.append(group_key)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY scheduled_at ASC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        cursor = await self._conn.execute(query, params)
        return [ScheduledTaskRecord.from_row(row) for row in await cursor.fetchall()]

    async def mark_scheduled_task_status(self, task_id: int, status: str) -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "UPDATE scheduled_tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
        await self._conn.commit()

    async def claim_scheduled_task(self, task_id: int) -> bool:
        """Atomically claim a pending task for one in-process timer."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE scheduled_tasks SET status = 'running' "
            "WHERE id = ? AND status = 'pending'",
            (task_id,),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def reschedule_running_task(
        self,
        task_id: int,
        *,
        scheduled_at: float,
        last_run_at: float,
    ) -> bool:
        """Return a running task to pending without resurrecting cancellation."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE scheduled_tasks SET status = 'pending', scheduled_at = ?, "
            "last_run_at = ? WHERE id = ? AND status = 'running'",
            (scheduled_at, last_run_at, task_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def defer_scheduled_task(self, task_id: int, *, scheduled_at: float) -> bool:
        """Defer a pending or claimed task unless it was explicitly stopped."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE scheduled_tasks SET status = 'pending', scheduled_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (scheduled_at, task_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def complete_running_task(self, task_id: int) -> bool:
        """Complete only the currently claimed run."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE scheduled_tasks SET status = 'done' "
            "WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def fail_running_task(self, task_id: int) -> bool:
        """Mark a claimed run as failed without overwriting pause/cancel."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "UPDATE scheduled_tasks SET status = 'error' "
            "WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def update_scheduled_task(
        self,
        task_id: int,
        *,
        status: str | None = None,
        scheduled_at: float | None = None,
        last_run_at: float | None = None,
    ) -> None:
        self._ensure_initialized()
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if scheduled_at is not None:
            fields.append("scheduled_at = ?")
            params.append(scheduled_at)
        if last_run_at is not None:
            fields.append("last_run_at = ?")
            params.append(last_run_at)
        if not fields:
            return
        params.append(task_id)
        await self._conn.execute(
            f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await self._conn.commit()

    async def create_delegated_task(
        self,
        *,
        run_id: str,
        conversation_id: str,
        prompt: str,
        workspace: str,
        report_token: str,
    ) -> None:
        self._ensure_initialized()
        await self._conn.execute(
            "INSERT INTO delegated_tasks "
            "(run_id, conversation_id, prompt, workspace, report_token, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (run_id, conversation_id, prompt, workspace, report_token),
        )
        await self._conn.commit()

    async def get_delegated_task(self, run_id: str) -> DelegatedTaskRecord | None:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM delegated_tasks WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        return DelegatedTaskRecord.from_row(row) if row else None

    async def get_recoverable_delegated_tasks(self) -> list[DelegatedTaskRecord]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT * FROM delegated_tasks WHERE status IN ('pending', 'running', 'reported') "
            "ORDER BY created_at ASC"
        )
        return [DelegatedTaskRecord.from_row(row) for row in await cursor.fetchall()]

    async def update_delegated_task(
        self,
        run_id: str,
        *,
        status: str | None = None,
        report: str | None = None,
        report_event_id: str | None = None,
        exit_code: int | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        self._ensure_initialized()
        fields: list[str] = []
        params: list[Any] = []
        for name, value in (
            ("status", status),
            ("report", report),
            ("report_event_id", report_event_id),
            ("exit_code", exit_code),
            ("started_at", started_at),
            ("finished_at", finished_at),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                params.append(value)
        if not fields:
            return
        params.append(run_id)
        await self._conn.execute(
            f"UPDATE delegated_tasks SET {', '.join(fields)} WHERE run_id = ?",
            params,
        )
        await self._conn.commit()

    async def accept_delegated_report(
        self,
        run_id: str,
        report_token: str,
        report: str,
        *,
        max_chars: int = 8000,
    ) -> bool:
        """Atomically accept the first valid report for a delegated run."""
        self._ensure_initialized()
        bounded = str(report or "").strip()
        if not bounded:
            return False
        bounded = bounded[:max(1, int(max_chars))]
        cursor = await self._conn.execute(
            "UPDATE delegated_tasks SET status = 'reported', report = ?, finished_at = strftime('%s', 'now') "
            "WHERE run_id = ? AND report_token = ? AND status IN ('pending', 'running', 'unreported')",
            (bounded, run_id, report_token),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    def _ensure_initialized(self) -> None:
        if self._conn is None:
            raise RuntimeError("MessageStore not initialized. Call initialize() first.")
