from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..message.sender import send_failure_message, send_succeeded

if TYPE_CHECKING:
    from ..message.sender import MessageSender, Peer


STATUS_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "简短告知用户接下来准备做什么。只写扁平纯文本，不要暴露详细思考过程。",
            "maxLength": 160,
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class StatusUpdateResult:
    ok: bool
    text: str
    result: object | None = None
    error: str = ""


async def send_status_update(
    args: dict,
    *,
    sender: "MessageSender",
    peer: "Peer",
) -> StatusUpdateResult:
    text = str(args.get("text", "")).strip()
    if not text:
        return StatusUpdateResult(ok=False, text="", error="text required")
    if len(text) > 160:
        return StatusUpdateResult(ok=False, text=text, error="text exceeds 160 characters")

    result = await sender.send(peer, text)
    if not send_succeeded(result):
        return StatusUpdateResult(
            ok=False,
            text=text,
            result=result,
            error=send_failure_message(result),
        )
    return StatusUpdateResult(ok=True, text=text, result=result)


async def status_update_tool(args: dict, *, sender: "MessageSender", peer: "Peer", **deps) -> str:
    """Send a short user-visible progress update without ending the pipeline."""
    outcome = await send_status_update(args, sender=sender, peer=peer)
    if outcome.ok:
        return "[OK] status update sent"
    return f"[Error: status update failed: {outcome.error}]"
