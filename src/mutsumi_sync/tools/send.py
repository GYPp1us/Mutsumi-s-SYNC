from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from .markdown_renderer import render_markdown_image
from ..message.sender import Peer, send_failure_message, send_succeeded

if TYPE_CHECKING:
    from ..config import Config
    from ..message.sender import MessageSender
    from ..memory.store import MessageStore

logger = logging.getLogger("mutsumi.tools.send")

MarkdownRenderer = Callable[..., Awaitable[str]]
_TARGET_RE = re.compile(r"^(private|group):([0-9]+)$")


def parse_send_target(value: object) -> tuple[str, Peer]:
    """Parse the model-facing canonical NapCat conversation target."""
    target = str(value or "").strip()
    match = _TARGET_RE.fullmatch(target)
    if match is None:
        raise ValueError(
            "target must use private:<numeric_id> or group:<numeric_id>"
        )
    kind, raw_id = match.groups()
    if int(raw_id) <= 0:
        raise ValueError("target id must be positive")
    canonical_target = f"{kind}:{int(raw_id)}"
    chat_type = 1 if kind == "private" else 2
    return canonical_target, Peer(chat_type=chat_type, peer_uid=str(int(raw_id)))

SEND_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "Optional text segment or text attached to a special segment. "
                "For ordinary text replies to the current conversation, use TO_USER. "
                "Text-only send is reserved for an explicitly different target."
            ),
        },
        "target": {
            "type": "string",
            "description": (
                "Required destination. Use exactly private:<numeric QQ user id> "
                "or group:<numeric QQ group id>. Do not use nicknames."
            ),
        },
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
    "required": ["target"],
    "additionalProperties": False,
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
    try:
        target_id, target_peer = parse_send_target(args.get("target"))
    except ValueError as exc:
        return f"[Error: invalid target: {exc}]"

    special_fields = (
        "media_id",
        "image",
        "image_url",
        "markdown_image",
        "face",
        "at_user",
        "reply_to",
        "forward",
    )
    has_special_segment = any(args.get(field) for field in special_fields)
    same_target = (
        peer is not None
        and target_peer.chat_type == peer.chat_type
        and target_peer.peer_uid == peer.peer_uid
    )
    if args.get("text") and not has_special_segment and same_target:
        return (
            "[Error: ordinary text for the current conversation must be written "
            "in TO_USER; use send.text only with a different target or a special segment]"
        )

    segments = []
    artifacts: list[dict] = []
    temporary_files: list[Path] = []
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
        temporary_files.append(Path(image_path))
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
        result = await sender.send(target_peer, segments)
        if not send_succeeded(result):
            return f"[Error: NapCat send failed: {send_failure_message(result)}]"
        if media_id and store is not None:
            await store.mark_media_used(media_id)
        if artifacts and store is not None:
            for artifact in artifacts:
                file_path = Path(str(artifact.get("file") or ""))
                if not file_path.is_file():
                    continue
                try:
                    media = await store.register_media(
                        file_path.read_bytes(),
                        kind="image",
                        ext=file_path.suffix,
                    )
                    artifact["media_id"] = media.media_id
                except Exception:
                    logger.exception("failed to register sent markdown image")
        if artifacts and isinstance(result, dict):
            result = dict(result)
            result["artifacts"] = artifacts
        if isinstance(result, dict):
            result = dict(result)
            result["target"] = {
                "conversation_id": target_id,
                "chat_type": target_peer.chat_type,
                "peer_uid": target_peer.peer_uid,
            }
        return json.dumps(result)
    except Exception as e:
        logger.exception("send_tool failed")
        return f"[Error: {e}]"
    finally:
        for path in temporary_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove temporary markdown image: %s", path)
