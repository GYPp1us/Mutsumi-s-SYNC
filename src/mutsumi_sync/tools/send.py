from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from .markdown_renderer import render_markdown_image
from ..message.sender import send_failure_message, send_succeeded

if TYPE_CHECKING:
    from ..config import Config
    from ..message.sender import MessageSender, Peer
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.send")

MarkdownRenderer = Callable[..., Awaitable[str]]

SEND_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Plain text reply"},
        "image": {"type": "string", "description": "Image file path"},
        "image_url": {"type": "string", "description": "Image URL"},
        "media_id": {
            "type": "string",
            "description": "Media Ledger 中的 media_id。优先用于复用已登记的图片或表情，不要自行填写服务器路径。",
        },
        "markdown_image": {
            "type": "string",
            "description": "Markdown source to render into a PNG image and send as an image segment",
        },
        "face": {"type": "integer", "description": "QQ face ID"},
        "at_user": {"type": "string", "description": "QQ user ID for group @ mention"},
        "reply_to": {"type": "integer", "description": "message_id to reply to"},
        "forward": {"type": "string", "description": "Forward message ID"},
    },
}


async def send_tool(
    args: dict,
    *,
    sender: "MessageSender",
    peer: "Peer",
    config: "Config | None" = None,
    store: "MessageStore | None" = None,
    markdown_renderer: MarkdownRenderer = render_markdown_image,
) -> str:
    """Execute send tool by building message segments and sending via sender."""
    segments = []
    artifacts: list[dict] = []
    media_id = str(args.get("media_id") or "").strip()
    if media_id:
        if store is None:
            return "[Error: media_id requires the media ledger]"
        media = await store.get_media(media_id)
        if media is None or media.status != "active":
            return f"[Error: media_id is unavailable: {media_id}]"
        if media.path:
            segments.append({"type": "image", "data": {"file": media.path}})
        elif media.source_url:
            segments.append({"type": "image", "data": {"url": media.source_url}})
        else:
            return f"[Error: media_id has no sendable source: {media_id}]"
    if args.get("text"):
        segments.append({"type": "text", "data": {"text": args["text"]}})
    if args.get("image"):
        segments.append({"type": "image", "data": {"file": args["image"]}})
    if args.get("image_url"):
        segments.append({"type": "image", "data": {"url": args["image_url"]}})
    if args.get("markdown_image"):
        if config is None:
            return "[Error: markdown image renderer requires config]"
        try:
            image_path = await markdown_renderer(args["markdown_image"], config=config)
        except Exception as e:
            logger.exception("markdown image render failed")
            return f"[Error: {e}]"
        segments.append({"type": "image", "data": {"file": image_path}})
        artifacts.append({
            "kind": "sent_image",
            "source": "markdown_image",
            "file": image_path,
            "markdown": args["markdown_image"],
        })
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
        if not send_succeeded(result):
            return f"[Error: NapCat send failed: {send_failure_message(result)}]"
        if media_id and store is not None:
            await store.mark_media_used(media_id)
        if artifacts and isinstance(result, dict):
            result = dict(result)
            result["artifacts"] = artifacts
        return json.dumps(result)
    except Exception as e:
        logger.exception("send_tool failed")
        return f"[Error: {e}]"
