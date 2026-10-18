from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..memory.timestamps import timestamp_memory_entry

if TYPE_CHECKING:
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.self_note")

SELF_NOTE_DESCRIPTION = (
    "仅在对当前发言者的关系定位、长期偏好或主观理解发生稳定变化时维护私人印象。"
    "不要保存普通事实、当前问题、工具搜索结果或一次性任务；只读回忆场景不要调用。"
    "add 追加，replace 全文覆盖。"
)

SELF_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "replace"],
            "description": "add: 追加到现有笔记末尾。replace: 全文覆盖。",
        },
        "content": {
            "type": "string",
            "description": "关系或长期主观理解的稳定变化。不得填写普通事实、搜索结果或一次性信息。建议 ≤1000 tokens。",
        },
    },
    "required": ["action", "content"],
}


async def self_note_tool(args: dict, *, store: "MessageStore", group_key: str) -> str:
    """Manage private self-note (add or replace)."""
    action = args.get("action", "add")
    content = args.get("content", "")

    if not content.strip():
        return "[Error: content required for self_note]"

    if action not in ("add", "replace"):
        return f"[Error: unknown action: {action}]"

    try:
        entry = timestamp_memory_entry(content)
        if action == "add":
            existing = await store.get_current_self_note(group_key)
            if existing:
                new_content = existing["content"] + "\n" + entry
            else:
                new_content = entry
        else:
            new_content = entry

        await store.upsert_self_note(group_key, new_content)
        return "[OK] self_note updated"
    except Exception as e:
        logger.exception("self_note_tool failed")
        return f"[Error: {e}]"
