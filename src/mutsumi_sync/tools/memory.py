from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.memory")

MEMORY_SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "要保存的事实或信息。一条一个事实。",
        },
    },
    "required": ["content"],
}

MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词（人名、事件、话题等）",
        },
        "limit": {
            "type": "integer",
            "description": "返回条数上限，默认 5",
        },
    },
    "required": ["query"],
}


async def memory_save(args: dict, *, store: "MessageStore", group_key: str) -> str:
    """Save a fact to long-term memory (category='memory')."""
    content = args.get("content", "")
    if not content.strip():
        return "[Error: content required for memory_save]"

    try:
        from ..memory.store import StoredMessage
        today = date.today().isoformat()
        msg_id = await store.save(StoredMessage(
            date=today,
            group_key=group_key,
            category="memory",
            content=content,
        ))
        return f"[OK] saved memory #{msg_id}"
    except Exception as e:
        logger.exception("memory_save failed")
        return f"[Error: {e}]"


async def memory_search(args: dict, *, store: "MessageStore", group_key: str) -> str:
    """Search long-term memory by keyword using FTS5."""
    query = args.get("query", "")
    limit = int(args.get("limit", 5))

    if not query.strip():
        return "[Error: query required for memory_search]"

    try:
        results = await store.search_memory(group_key, query, limit)
        if not results:
            return "[OK] no matching memories found"

        lines = []
        for r in results:
            preview = r["content"][:300]
            lines.append(f"[{r['date']}] {preview}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("memory_search failed")
        return f"[Error: {e}]"
