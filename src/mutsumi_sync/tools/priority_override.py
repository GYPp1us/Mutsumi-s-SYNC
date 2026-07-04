from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..memory.timestamps import timestamp_memory_entry

if TYPE_CHECKING:
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.priority_override")

PRIORITY_OVERRIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "replace", "clear"],
            "description": "add appends, replace overwrites, clear removes active override text.",
        },
        "content": {
            "type": "string",
            "description": "High-priority instruction that should be repeated in every user input.",
        },
    },
    "required": ["action"],
}


async def priority_override_tool(args: dict, *, store: "MessageStore", group_key: str) -> str:
    action = args.get("action", "add")
    content = str(args.get("content", ""))

    if action not in ("add", "replace", "clear"):
        return f"[Error: unknown action: {action}]"
    if action != "clear" and not content.strip():
        return "[Error: content required for priority_override]"

    try:
        if action == "clear":
            await store.upsert_priority_override(group_key, "")
            return "[OK] priority_override cleared"

        entry = timestamp_memory_entry(content)
        if action == "add":
            existing = await store.get_current_priority_override(group_key)
            if existing and existing.get("content"):
                new_content = f"{existing['content']}\n{entry}"
            else:
                new_content = entry
        else:
            new_content = entry

        await store.upsert_priority_override(group_key, new_content)
        return "[OK] priority_override updated"
    except Exception as e:
        logger.exception("priority_override_tool failed")
        return f"[Error: {e}]"
