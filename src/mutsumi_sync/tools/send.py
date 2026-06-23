from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..message.sender import MessageSender, Peer

logger = logging.getLogger("mutsumi.tools.send")

SEND_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "纯文本回复"},
        "image": {"type": "string", "description": "图片 file 路径"},
        "image_url": {"type": "string", "description": "图片 URL"},
        "face": {"type": "integer", "description": "QQ 小表情 ID"},
        "at_user": {"type": "string", "description": "QQ 用户 ID，群聊 @人"},
        "reply_to": {"type": "integer", "description": "引用消息的 message_id"},
        "forward": {"type": "string", "description": "转发消息 ID"},
    },
}


async def send_tool(args: dict, *, sender: "MessageSender", peer: "Peer") -> str:
    """Execute send tool — builds message segments and sends via sender."""
    segments = []
    if args.get("text"):
        segments.append({"type": "text", "data": {"text": args["text"]}})
    if args.get("image"):
        segments.append({"type": "image", "data": {"file": args["image"]}})
    if args.get("image_url"):
        segments.append({"type": "image", "data": {"url": args["image_url"]}})
    if args.get("face"):
        segments.append({"type": "face", "data": {"id": str(args["face"])}})
    if args.get("at_user"):
        segments.append({"type": "at", "data": {"qq": args["at_user"]}})
    if args.get("reply_to"):
        segments.append({"type": "reply", "data": {"id": str(args["reply_to"])}})
    if args.get("forward"):
        segments.append({"type": "forward", "data": {"id": args["forward"]}})

    if not segments:
        return "[Error: send tool called with no content]"

    try:
        result = await sender.send(peer, segments)
        return json.dumps(result)
    except Exception as e:
        logger.exception("send_tool failed")
        return f"[Error: {e}]"
