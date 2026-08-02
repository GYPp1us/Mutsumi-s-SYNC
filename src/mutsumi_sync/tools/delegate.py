from __future__ import annotations

from typing import Any


DELEGATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": (
                "Natural-language task for the external OpenCode agent. "
                "Include any skill file path and delivery requirements in the prompt."
            ),
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


async def delegate_task_tool(
    args: dict[str, Any],
    *,
    scheduler,
    conversation_id: str,
    group_key: str,
    **_: Any,
) -> str:
    """Start an asynchronous OpenCode run bound to the current conversation."""
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "[Error: prompt is required]"
    bound_conversation = str(conversation_id or group_key).strip()
    if not bound_conversation:
        return "[Error: no conversation is bound to this task]"
    try:
        run_id = await scheduler.delegate_task(
            prompt=prompt,
            conversation_id=bound_conversation,
        )
    except Exception as exc:
        return f"[Error: could not start OpenCode task: {exc}]"
    return (
        f"[OK] OpenCode task started run_id={run_id}. "
        "It must call report_task with a concise natural-language result when complete."
    )
