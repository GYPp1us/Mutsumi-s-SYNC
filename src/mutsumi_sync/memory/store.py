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

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> StoredMessage:
        return cls(
            id=row["id"],
            date=row["date"],
            group_key=row["group_key"],
            category=row["category"],
            content=row["content"],
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

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("MessageStore closed")

    def _ensure_initialized(self) -> None:
        if self._conn is None:
            raise RuntimeError("MessageStore not initialized. Call initialize() first.")
