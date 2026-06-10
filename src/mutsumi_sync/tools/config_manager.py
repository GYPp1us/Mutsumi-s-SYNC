from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger("mutsumi.tools.config_manager")

CONFIG_MANAGER_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "set", "list", "reload"],
        },
        "key": {"type": "string", "description": "dot-path, e.g. model.temperature"},
        "value": {"description": "新值 (仅 action=set 时需要)"},
    },
    "required": ["action"],
}


async def config_manager(args: dict, *, config: "Config") -> str:
    action = args.get("action", "get")
    key = args.get("key", "")
    value = args.get("value")

    if action == "get":
        if not key:
            return "[Error: key required for get]"
        result = config.get(key)
        return str(result)

    elif action == "set":
        if not key or value is None:
            return "[Error: key and value required for set]"
        result = config.set(key, value)
        if result.startswith("[OK]"):
            config.save()
        return result

    elif action == "list":
        sections = [
            k for k, v in config.model_fields.items()
            if not k.startswith("_") and k not in ("system_prompt", "dirty")
        ]
        return f"Available sections: {', '.join(sections)}"

    elif action == "reload":
        return config.reload()

    return f"[Error: unknown action: {action}]"
