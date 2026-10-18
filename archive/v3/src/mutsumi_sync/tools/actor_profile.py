from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..memory.store import ActorRecord

if TYPE_CHECKING:
    from ..memory.store import MessageStore


ACTOR_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "list", "update"],
            "description": "读取、列出或更新一个全局参与者档案。",
        },
        "actor_id": {
            "type": "string",
            "description": "稳定 actor_id；只能使用上下文或工具结果中出现的 ID。",
        },
        "private_alias": {
            "type": "string",
            "description": "Bot 私下使用的称呼。",
        },
        "relationship": {
            "type": "string",
            "description": "与 Bot 或主人的关系标签。",
        },
    },
    "required": ["action"],
}


def _render(actor: ActorRecord) -> str:
    return json.dumps(
        {
            "actor_id": actor.actor_id,
            "kind": actor.kind,
            "platform": actor.platform,
            "display_name": actor.display_name,
            "private_alias": actor.private_alias,
            "relationship": actor.relationship,
        },
        ensure_ascii=False,
    )


async def actor_profile_tool(args: dict, *, store: "MessageStore", **deps) -> str:
    action = str(args.get("action", "get"))
    if action == "list":
        actors = await store.list_actors()
        return json.dumps([json.loads(_render(actor)) for actor in actors], ensure_ascii=False)

    actor_id = str(args.get("actor_id", "")).strip()
    if not actor_id:
        return "[Error: actor_id required]"

    actor = await store.get_actor(actor_id)
    if actor is None:
        return f"[Error: unknown actor_id: {actor_id}]"
    if action == "get":
        return _render(actor)
    if action != "update":
        return f"[Error: unknown actor_profile action: {action}]"

    alias = args.get("private_alias")
    relationship = args.get("relationship")
    if alias is None and relationship is None:
        return "[Error: update requires private_alias or relationship]"
    updated = await store.update_actor_profile(
        actor_id,
        private_alias=str(alias) if alias is not None else None,
        relationship=str(relationship) if relationship is not None else None,
    )
    return "[OK] actor profile updated\n" + (_render(updated) if updated else actor_id)
