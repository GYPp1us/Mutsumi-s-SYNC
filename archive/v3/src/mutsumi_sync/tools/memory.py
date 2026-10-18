from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.memory")

MEMORY_SAVE_DESCRIPTION = (
    "仅在用户明确要求记住，或出现新的、稳定的长期事实时保存一条记忆。"
    "不要保存当前查询、临时任务、memory_search 的返回结果、已经存在的事实或仅用于本轮回答的信息；"
    "用户只是在询问或回忆时不要调用。"
)

MEMORY_SEARCH_DESCRIPTION = (
    "只读搜索已经保存的长期记忆。用于回答用户的回忆或事实查询；"
    "搜索后不要为了整理、确认或重存结果而调用 memory_save 或 self_note。"
)

MEMORY_SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "要保存的一条新的稳定长期事实，不得填写搜索结果、当前问题或重复事实。",
        },
    },
    "required": ["content"],
}

MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "只读搜索关键词（人名、事件、话题等）。搜索本身不需要伴随任何记忆写入。",
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
