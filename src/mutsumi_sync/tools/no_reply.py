from __future__ import annotations

NO_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Internal reason for intentionally sending no user-visible reply",
        },
    },
    "additionalProperties": False,
}


async def no_reply_tool(args: dict) -> str:
    """A control tool that marks the current turn as intentionally silent."""
    reason = str(args.get("reason", "")).strip()
    if reason:
        return f"[OK] no reply: {reason}"
    return "[OK] no reply"
