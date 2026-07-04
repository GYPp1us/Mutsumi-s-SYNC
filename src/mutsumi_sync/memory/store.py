from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("mutsumi.store")


class MessageCategory(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    MIXED = "mixed"


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
            created_at  REAL    NOT NULL DEFAULT (julianday('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_group
            ON summaries(group_key, seq);

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
    """

    def __init__(self, db_path: str = "data/mutsumi.db", media_dir: str = "data/media"):
        self.db_path = Path(db_path)
        self.media_dir = Path(media_dir)
        self._conn: aiosqlite.Connection | None = None

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

        filename = f"{datetime.now().strftime('%H%M%S%f')}{'.' + ext if ext else ''}"
        filepath = dest_dir / filename
        filepath.write_bytes(data)

        content = json.dumps({"file": str(filepath), "size": len(data)})
        return await self.save(StoredMessage(
            date=today, group_key=group_key, category=category, content=content,
        ))

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

    async def add_summary(self, group_key: str, source: str, summary: str, last_message_id: int = 0) -> int:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM summaries WHERE group_key = ?",
            (group_key,)
        )
        row = await cursor.fetchone()
        next_seq = row[0] if row else 1

        cursor = await self._conn.execute(
            "INSERT INTO summaries (group_key, seq, source, summary, last_message_id) VALUES (?, ?, ?, ?, ?)",
            (group_key, next_seq, source, summary, last_message_id),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_summaries(self, group_key: str, limit: int = 180) -> list[dict]:
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, group_key, seq, source, summary, last_message_id, created_at FROM summaries WHERE group_key = ? ORDER BY seq ASC LIMIT ?",
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
            logger.info("MessageStore closed")

    async def get_message_group_keys(self) -> list[str]:
        """Return all distinct group_keys from messages table."""
        self._ensure_initialized()
        cursor = await self._conn.execute("SELECT DISTINCT group_key FROM messages")
        rows = await cursor.fetchall()
        return [r["group_key"] for r in rows]

    async def get_newest_summary(self, group_key: str) -> dict | None:
        """Return the summary with the highest last_message_id for a group."""
        self._ensure_initialized()
        cursor = await self._conn.execute(
            "SELECT id, seq, source, summary, last_message_id FROM summaries "
            "WHERE group_key = ? ORDER BY last_message_id DESC LIMIT 1",
            (group_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "seq": row["seq"], "source": row["source"],
                "summary": row["summary"], "last_message_id": row["last_message_id"]}

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

    def _ensure_initialized(self) -> None:
        if self._conn is None:
            raise RuntimeError("MessageStore not initialized. Call initialize() first.")
