from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.store import MessageStore

BOT_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "replace", "clear"]},
        "content": {"type": "string", "description": "Facts about the bot's own identity, experience, values, or long-term plans."},
    },
    "required": ["action"],
}


async def bot_state_tool(args: dict, *, store: "MessageStore", **deps) -> str:
    action = str(args.get("action", "add"))
    content = str(args.get("content", "")).strip()
    if action == "clear":
        await store.clear_canonical_state()
        return "[OK] global bot state cleared"
    if not content:
        return "[Error: content required for bot_state]"
    if action == "add":
        current = await store.get_canonical_state()
        if current and current.get("content"):
            content = str(current["content"]).rstrip() + "\n" + content
    elif action != "replace":
        return f"[Error: unknown bot_state action: {action}]"
    version = await store.upsert_canonical_state(content)
    return f"[OK] global bot state staged version {version}"
